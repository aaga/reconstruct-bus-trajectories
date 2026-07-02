"""Shared helpers for the dense-VTRAK / ROCKET validation figures.

``build_rocket_vs_r2.py`` and ``build_vtrak_smooth.py`` (and the scripts that
import from the latter) all need the same primitives: load the dense ROCKET CSV
(naive Chicago local → UTC), load R2 BusTime hour-files, pick one complete trip
inside a time window, and choose the best-fitting GTFS shape by minimum median
perpendicular snap distance. They live here so the loaders aren't copy-pasted.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from .gtfs import load_gtfs_shape_with_dist
from core.mapmatch import get_matcher

DEFAULT_MAX_PERP_M = 50.0


def chicago_to_utc(naive_local: pd.Series) -> pd.Series:
    """Localize a naive Chicago-local datetime series to UTC (DST-safe).

    Ambiguous/nonexistent wall-clock times (DST boundaries) become NaT.
    """
    t = pd.to_datetime(naive_local)
    return t.dt.tz_localize(
        "America/Chicago", ambiguous="NaT", nonexistent="NaT"
    ).dt.tz_convert("UTC")


def load_rocket_csv(path: str | Path, veh_ids=None) -> pd.DataFrame:
    """Load the dense ROCKET/VTRAK CSV, adding a UTC ``ts_utc`` column.

    ROCKET timestamps are naive Chicago local. If ``veh_ids`` is given, keep
    only those ``VEH_ID``s. Rows whose timestamp can't be localized are dropped.
    """
    df = pd.read_csv(path)
    if veh_ids is not None:
        df = df[df.VEH_ID.astype(str).isin(set(veh_ids))].copy()
    df["ts_utc"] = chicago_to_utc(df.AVL_EVENT_TIME)
    return df.dropna(subset=["ts_utc"]).sort_values(["VEH_ID", "ts_utc"])


def load_r2_hours(hour_files, veh_ids=None) -> pd.DataFrame:
    """Concatenate R2 parquet hour-files, dedupe on (vehicle_id, timestamp).

    If ``veh_ids`` is given, keep only those vehicles.
    """
    df = pd.concat(
        [pq.read_table(str(f)).to_pandas() for f in hour_files], ignore_index=True
    )
    if veh_ids is not None:
        df = df[df.vehicle_id.isin(set(veh_ids))].copy()
    return (
        df.drop_duplicates(["vehicle_id", "timestamp"])
        .sort_values(["vehicle_id", "timestamp"])
        .reset_index(drop=True)
    )


def build_shape_matcher(gtfs_zip: str | Path, shape_id: str, max_perp_m: float = DEFAULT_MAX_PERP_M):
    """A SnapToShape matcher for one GTFS shape."""
    poly, dist = load_gtfs_shape_with_dist(gtfs_zip, shape_id)
    kw = {"polyline_latlon": poly, "max_perp_m": max_perp_m}
    if dist is not None:
        kw["dist_along_m_per_vertex"] = dist
    return get_matcher("shape_snap", **kw)


def pick_trip_in_window(g: pd.DataFrame, window) -> pd.DataFrame:
    """Pick a complete trip fully inside ``window`` (most pings); fallback = most pings."""
    lo, hi = window
    best = None
    for _, tg in g.groupby("trip_id"):
        t0, t1 = tg.timestamp.min(), tg.timestamp.max()
        if t0 < lo or t1 > hi:
            continue
        if best is None or len(tg) > len(best):
            best = tg
    if best is None:  # fallback: most pings regardless of containment
        best = max((tg for _, tg in g.groupby("trip_id")), key=len)
    return best.sort_values("timestamp").reset_index(drop=True)


def best_shape(trip: pd.DataFrame, shape_ids, gtfs_zip: str | Path, max_perp_m: float = DEFAULT_MAX_PERP_M):
    """Among ``shape_ids``, pick the one a trip snaps to best.

    Returns ``(shape_id, median_perp_m, coverage, matcher)`` for the candidate
    with on-route coverage > 0.5 and the lowest median perpendicular distance,
    or the first candidate if none qualify.
    """
    lats = trip.latitude.to_numpy()
    lons = trip.longitude.to_numpy()
    best = None
    for sid in shape_ids:
        m = build_shape_matcher(gtfs_zip, sid, max_perp_m)
        res = m.match(lats, lons)
        on = res.on_route
        score = np.median(res.perp_dist_m[on]) if on.sum() else np.inf
        cov = on.mean()
        if best is None or (cov > 0.5 and score < best[1]):
            best = (sid, score, cov, m)
    return best
