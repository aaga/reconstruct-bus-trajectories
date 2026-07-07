"""End-to-end reconstruction pipeline."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .mapmatch import MapMatcher, MatchResult
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
