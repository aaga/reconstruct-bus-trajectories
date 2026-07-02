"""Shared client for the public R2 CTA AVL archive.

The archive (produced by the companion ``scrape-bus-pings`` scraper) is a set of
Hive-partitioned hourly Parquet objects on a public Cloudflare R2 bucket, indexed
by ``_manifest.parquet``. Everything is UTC.

Historically every figure/data-prep script re-implemented its own copy of the
bucket URL, a curl-based ``fetch``, manifest loading, and the AVL-CSV
conversion. This module is the single home for all of that; scripts import from
here instead of hand-rolling it.

Downloads are cached under the repo's gitignored ``caches/realtime_archive/``.
"""

from __future__ import annotations

import datetime as _dt
import subprocess
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

ARCHIVE_URL = "https://pub-777d0904efb449dc838791645b9e2e0f.r2.dev"

_REPO = Path(__file__).resolve().parents[2]
CACHE_DIR = _REPO / "caches" / "realtime_archive"


def fetch(url: str, dst: str | Path) -> Path:
    """Download ``url`` to ``dst`` (via curl, robust with R2 public URLs).

    Skips the download if a non-empty file is already cached at ``dst``.
    """
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and dst.stat().st_size > 0:
        return dst
    print(f"  ↓ {url}")
    subprocess.check_call(["curl", "-sSL", "-o", str(dst), url])
    return dst


def load_manifest(cache_dir: str | Path = CACHE_DIR, refresh: bool = False) -> pd.DataFrame:
    """The archive manifest (agency, year, month, day, hour, path, ...)."""
    cache_dir = Path(cache_dir)
    local = cache_dir / "_manifest.parquet"
    if refresh and local.exists():
        local.unlink()
    fetch(f"{ARCHIVE_URL}/_manifest.parquet", local)
    return pq.read_table(local).to_pandas()


def cta_manifest(cache_dir: str | Path = CACHE_DIR, refresh: bool = False) -> pd.DataFrame:
    """CTA rows of the manifest, sorted by UTC hour, with a ``dt`` column added."""
    m = load_manifest(cache_dir, refresh)
    cta = m[m.agency == "cta"].copy()
    cta["dt"] = pd.to_datetime(
        dict(year=cta.year, month=cta.month, day=cta.day, hour=cta.hour), utc=True
    )
    return cta.sort_values("dt").reset_index(drop=True)


def load_hour(path: str, cache_dir: str | Path = CACHE_DIR) -> pd.DataFrame:
    """One hourly object, cached locally by flattened path."""
    # Flatten the hive-style path into a single filename so pyarrow doesn't
    # auto-treat the cache dir as a partitioned dataset.
    local = Path(cache_dir) / path.replace("/", "__")
    fetch(f"{ARCHIVE_URL}/{path}", local)
    return pq.ParquetFile(local).read().to_pandas()


def load_hours(
    rows: pd.DataFrame,
    cache_dir: str | Path = CACHE_DIR,
    route_id: str | None = None,
) -> pd.DataFrame:
    """Concatenate the hour-files named by ``rows.path`` (manifest rows).

    If ``route_id`` is given, each hour is filtered to that route before
    concatenation (keeps memory down when scouring the whole archive).
    """
    parts: list[pd.DataFrame] = []
    for _, row in rows.iterrows():
        df = load_hour(row.path, cache_dir)
        if route_id is not None:
            df = df[df.route_id == str(route_id)]
        parts.append(df)
    return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()


def load_recent_cta_hours(
    n_hours: int, cache_dir: str | Path = CACHE_DIR, route_id: str | None = None
) -> pd.DataFrame:
    """Concatenated CTA pings from the ``n_hours`` most recent hour-files."""
    cta = cta_manifest(cache_dir).sort_values("dt", ascending=False).head(n_hours)
    if cta.empty:
        raise SystemExit("no CTA hours in manifest")
    return load_hours(cta, cache_dir, route_id)


def load_cta_hours_from(
    start_utc: pd.Timestamp, cache_dir: str | Path = CACHE_DIR, route_id: str | None = None
) -> pd.DataFrame:
    """Concatenated CTA pings from every hour-file at/after ``start_utc``."""
    cta = cta_manifest(cache_dir)
    cta = cta[cta.dt >= start_utc]
    return load_hours(cta, cache_dir, route_id)


def load_all_cta_hours(
    cache_dir: str | Path = CACHE_DIR, route_id: str | None = None
) -> pd.DataFrame:
    """Concatenated CTA pings from the entire archive (optionally one route)."""
    return load_hours(cta_manifest(cache_dir), cache_dir, route_id)


def _utc_hours(start_ms: int, end_ms: int, pad_h: int = 1) -> list[tuple[int, int, int, int]]:
    """The ``(year, month, day, hour)`` UTC hours a ``[start, end]`` window spans."""
    start = _dt.datetime.fromtimestamp(start_ms / 1000, _dt.UTC) - _dt.timedelta(hours=pad_h)
    end = _dt.datetime.fromtimestamp(end_ms / 1000, _dt.UTC) + _dt.timedelta(hours=pad_h)
    hours, cur = [], start.replace(minute=0, second=0, microsecond=0)
    while cur <= end:
        hours.append((cur.year, cur.month, cur.day, cur.hour))
        cur += _dt.timedelta(hours=1)
    return hours


def trip_avl_pings(
    route_id: str,
    vehicle_id: str,
    trip_id: str,
    start_ms: int,
    end_ms: int,
    manifest: pd.DataFrame | None = None,
    cache_dir: str | Path = CACHE_DIR,
) -> pd.DataFrame:
    """Archive pings for one trip, in ``load_avl_csv``'s column shape.

    Matches an observed trip by ``(route_id, vehicle_id, trip_id)`` over the UTC
    hours its wall-clock window spans (``trip_id`` is the BusTime trip id, so it
    isolates exactly the rider's trip). Returns columns: trip_id, bus_id,
    route_id, avl_event_time (UTC, ``"%Y-%m-%d %H:%M:%S.%f"``), latitude,
    longitude, heading, plus epoch_ms. Empty frame if the trip isn't found.
    """
    man = manifest if manifest is not None else load_manifest(cache_dir)
    cta = man[man.agency == "cta"]
    frames = []
    for (y, mo, d, h) in _utc_hours(start_ms, end_ms):
        row = cta[(cta.year == y) & (cta.month == mo) & (cta.day == d) & (cta.hour == h)]
        if not row.empty:
            frames.append(load_hour(row.iloc[0]["path"], cache_dir))
    if not frames:
        return pd.DataFrame()

    allp = pd.concat(frames, ignore_index=True)
    sub = allp[
        (allp.route_id == str(route_id))
        & (allp.vehicle_id == str(vehicle_id))
        & (allp.trip_id == str(trip_id))
    ].copy()
    if sub.empty:
        return sub

    ts = pd.to_datetime(sub["timestamp"], utc=True).dt.tz_convert(None)
    out = pd.DataFrame({
        "trip_id": sub["trip_id"].astype(str),
        "bus_id": sub["vehicle_id"].astype(str),
        "route_id": sub["route_id"].astype(str),
        "avl_event_time": ts.dt.strftime("%Y-%m-%d %H:%M:%S.%f"),
        "latitude": sub["latitude"].astype(float),
        "longitude": sub["longitude"].astype(float),
        "heading": sub.get("bearing"),
        # Force ns first: the archive column may be datetime64[ms] or [us], and
        # a raw astype(int64) would otherwise yield the wrong scale.
        "epoch_ms": (ts.astype("datetime64[ns]").astype("int64") // 1_000_000),
    })
    return out.sort_values("epoch_ms").reset_index(drop=True)


def to_avl_csv_format(
    df: pd.DataFrame, out_csv: str | Path, pattern_id: str = "3936"
) -> Path:
    """Write R2 pings as a CSV in the format ``load_avl_csv`` expects.

    ``load_avl_csv`` only reads: trip_id, bus_id, route_id, pattern_id,
    avl_event_time, latitude, longitude. The remaining canonical AVL columns are
    filled with empty strings to match the parser's expectations.
    """
    out_csv = Path(out_csv)
    df = df.copy()
    df["avl_event_time"] = (
        df.timestamp.dt.tz_convert(None).dt.strftime("%Y-%m-%d %H:%M:%S.%f")
    )
    df["bus_id"] = df.vehicle_id
    df["pattern_id"] = pattern_id

    out = pd.DataFrame({
        "id": df.entity_id.fillna("") + "_" + df.timestamp.astype(str),
        "bus_id": df.bus_id,
        "avl_event_time": df.avl_event_time,
        "bt_ver": "",
        "route_id": df.route_id,
        "pattern_id": df.pattern_id,
        "direction": "",
        "deviation": "",
        "speed": "",
        "operator_id": "",
        "last_ob_update": "",
        "garage": "",
        "run_id": "",
        "trip_id": df.trip_id,
        "last_trip_update": "",
        "last_tp_passed": "",
        "last_tp_update": "",
        "latitude": df.latitude,
        "longitude": df.longitude,
        "heading": df.bearing.fillna("").astype(str),
        "onroute": "",
        "mmode": "",
        "last_mmode": "",
        "cta_inserted_dtm_usa_chi": "",
        "service_yearmo": "",
    })
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(out_csv, index=False)
    return out_csv
