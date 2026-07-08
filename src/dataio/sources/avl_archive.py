"""Public R2 realtime archive → canonical trace for one observed trip.

Wraps :func:`dataio.realtime.trip_avl_pings` (which isolates a trip by
``(route_id, vehicle_id, trip_id)`` over the UTC hours its window spans) and
stamps the GTFS ``pattern_id`` the archive rows don't carry. Requires the
archive hour-file cache (fetched on demand from the public R2 bucket).

spec: {"kind": "avl_archive", "route_id", "vehicle_id", "trip_id",
       "start_ms", "end_ms", "pattern_id", "cache_dir"?}
"""

from __future__ import annotations

import pandas as pd

from dataio.realtime import CACHE_DIR, trip_avl_pings
from . import empty_canonical


def load(spec: dict) -> pd.DataFrame:
    df = trip_avl_pings(
        spec["route_id"],
        spec["vehicle_id"],
        spec["trip_id"],
        spec["start_ms"],
        spec["end_ms"],
        cache_dir=spec.get("cache_dir", CACHE_DIR),
    )
    if df.empty:
        return empty_canonical()
    df = df.copy()
    df["pattern_id"] = str(spec.get("pattern_id", ""))
    return df
