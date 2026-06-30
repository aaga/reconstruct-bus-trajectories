"""Shared client for the public R2 CTA AVL archive.

The archive (produced by the companion ``scrape-bus-pings`` scraper) is a set of
Hive-partitioned hourly Parquet objects on a public Cloudflare R2 bucket, indexed
by ``_manifest.parquet``. Everything is UTC.

Historically every figure/data-prep script re-implemented its own copy of the
bucket URL, a curl-based ``fetch``, manifest loading, and the AVL-CSV
conversion. This module is the single home for all of that; scripts import from
here instead of hand-rolling it.

Downloads are cached under the repo's gitignored ``caches/r2_cache/``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

R2_PUB = "https://pub-777d0904efb449dc838791645b9e2e0f.r2.dev"

_REPO = Path(__file__).resolve().parents[2]
CACHE_DIR = _REPO / "caches" / "r2_cache"


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
    fetch(f"{R2_PUB}/_manifest.parquet", local)
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
    fetch(f"{R2_PUB}/{path}", local)
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
