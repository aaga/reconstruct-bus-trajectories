"""Fetch CTA AVL pings for one observed trip from the public R2 archive.

The archive (produced by the companion scraper) is a set of Hive-partitioned
hourly Parquet objects on a public Cloudflare R2 bucket, indexed by
``_manifest.parquet``. Everything here works in UTC: archive ``timestamp`` is
UTC, and we match an observed trip by ``(route_id, vehicle_id, trip_id)`` over
the UTC hours its wall-clock window spans. ``trip_id`` is the BusTime trip id
(== the phone app's ``tatripid``), so it isolates exactly the rider's trip.

Downloads are cached under the repo's gitignored ``r2_cache/``.
"""

from __future__ import annotations

import datetime as dt
import io
import urllib.request
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

R2_PUB = "https://pub-777d0904efb449dc838791645b9e2e0f.r2.dev"
REPO = Path(__file__).resolve().parents[2]
CACHE = REPO / "caches" / "r2_cache"
_UA = {"User-Agent": "bus-trajectories/analysis (research)"}


def _fetch(url: str, timeout: float = 120) -> bytes:
    return urllib.request.urlopen(urllib.request.Request(url, headers=_UA), timeout=timeout).read()


def load_manifest(refresh: bool = True) -> pd.DataFrame:
    """The archive manifest (agency, year, month, day, hour, path, ...)."""
    CACHE.mkdir(exist_ok=True)
    local = CACHE / "_manifest.parquet"
    if refresh or not local.exists():
        local.write_bytes(_fetch(f"{R2_PUB}/_manifest.parquet"))
    return pq.read_table(local).to_pandas()


def _fetch_hour(path: str) -> pd.DataFrame:
    """One hourly object, cached locally by flattened path."""
    local = CACHE / path.replace("/", "__")
    if not local.exists():
        local.write_bytes(_fetch(f"{R2_PUB}/{path}"))
    return pq.read_table(local).to_pandas()


def _utc_hours(start_ms: int, end_ms: int, pad_h: int = 1) -> list[tuple[int, int, int, int]]:
    start = dt.datetime.utcfromtimestamp(start_ms / 1000) - dt.timedelta(hours=pad_h)
    end = dt.datetime.utcfromtimestamp(end_ms / 1000) + dt.timedelta(hours=pad_h)
    hours, cur = [], start.replace(minute=0, second=0, microsecond=0)
    while cur <= end:
        hours.append((cur.year, cur.month, cur.day, cur.hour))
        cur += dt.timedelta(hours=1)
    return hours


def trip_avl_pings(
    route_id: str,
    vehicle_id: str,
    trip_id: str,
    start_ms: int,
    end_ms: int,
    manifest: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Archive pings for one trip, in load_avl_csv's column shape.

    Returns columns: trip_id, bus_id, route_id, avl_event_time (UTC,
    "%Y-%m-%d %H:%M:%S.%f"), latitude, longitude, heading, plus epoch_ms.
    Empty frame if the trip isn't found.
    """
    man = manifest if manifest is not None else load_manifest()
    cta = man[man.agency == "cta"]
    frames = []
    for (y, mo, d, h) in _utc_hours(start_ms, end_ms):
        row = cta[(cta.year == y) & (cta.month == mo) & (cta.day == d) & (cta.hour == h)]
        if not row.empty:
            frames.append(_fetch_hour(row.iloc[0]["path"]))
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
