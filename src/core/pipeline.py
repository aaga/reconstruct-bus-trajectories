"""End-to-end reconstruction pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from dataio.io import load_avl_csv, load_gtfs_shape_with_dist, shape_id_for_pattern
from .mapmatch import MapMatcher, MatchResult, get_matcher
from .smooth import LocregPchipResult, locreg_pchip


@dataclass
class TripMeta:
    trip_id: str
    bus_id: str
    route_id: str
    pattern_id: str
    n_pings: int
    n_on_route: int
    first_ping: pd.Timestamp
    last_ping: pd.Timestamp


@dataclass
class TripReconstruction:
    meta: TripMeta
    t: np.ndarray  # seconds since first ping
    d: np.ndarray  # meters along shape (raw, on-route only)
    match: MatchResult  # full match result for all pings (incl. off-route)
    on_route_mask: np.ndarray  # bool, len == n_pings
    smoothed: LocregPchipResult


def reconstruct_trip(
    trip_df: pd.DataFrame,
    matcher: MapMatcher,
    bandwidth: int = 20,
    degree: int = 3,
) -> TripReconstruction:
    """Reconstruct one trip's smooth trajectory."""
    if trip_df.empty:
        raise ValueError("trip_df is empty")

    trip_df = trip_df.sort_values("avl_event_time").reset_index(drop=True)
    lats = trip_df["latitude"].to_numpy()
    lons = trip_df["longitude"].to_numpy()
    match = matcher.match(lats, lons)
    on = match.on_route

    times = trip_df["avl_event_time"].to_numpy()
    t_full = (times - times[0]).astype("timedelta64[ms]").astype(float) / 1000.0

    if on.sum() < 2:
        raise ValueError(
            f"trip {trip_df['trip_id'].iloc[0]} has <2 on-route pings; cannot smooth"
        )
    t = t_full[on]
    d = match.dist_along_m[on]

    smoothed = locreg_pchip(t, d, bandwidth=bandwidth, degree=degree)

    meta = TripMeta(
        trip_id=str(trip_df["trip_id"].iloc[0]),
        bus_id=str(trip_df["bus_id"].iloc[0]),
        route_id=str(trip_df["route_id"].iloc[0]),
        pattern_id=str(trip_df["pattern_id"].iloc[0]),
        n_pings=len(trip_df),
        n_on_route=int(on.sum()),
        first_ping=pd.Timestamp(times[0]),
        last_ping=pd.Timestamp(times[-1]),
    )
    return TripReconstruction(
        meta=meta,
        t=t,
        d=d,
        match=match,
        on_route_mask=on,
        smoothed=smoothed,
    )


def reconstruct_csv(
    csv_path: str | Path,
    gtfs_zip_path: str | Path,
    route_id: str,
    pattern_id: str,
    matcher_name: str = "shape_snap",
    max_perp_m: float = 50.0,
    bandwidth: int = 20,
    degree: int = 3,
) -> dict[str, TripReconstruction]:
    """Reconstruct every trip on ``route_id`` + ``pattern_id`` in ``csv_path``.

    Returns a dict keyed by ``trip_id``.
    """
    df = load_avl_csv(csv_path)
    df = df[(df["route_id"] == route_id) & (df["pattern_id"] == pattern_id)]
    if df.empty:
        raise ValueError(
            f"no rows match route_id={route_id!r}, pattern_id={pattern_id!r} in {csv_path}"
        )

    shape, dist_along = load_gtfs_shape_with_dist(
        gtfs_zip_path, shape_id_for_pattern(pattern_id)
    )
    matcher_kwargs = {"polyline_latlon": shape, "max_perp_m": max_perp_m}
    if matcher_name == "shape_snap" and dist_along is not None:
        matcher_kwargs["dist_along_m_per_vertex"] = dist_along
    matcher = get_matcher(matcher_name, **matcher_kwargs)

    out: dict[str, TripReconstruction] = {}
    for trip_id, grp in df.groupby("trip_id"):
        out[str(trip_id)] = reconstruct_trip(
            grp, matcher, bandwidth=bandwidth, degree=degree
        )
    return out
