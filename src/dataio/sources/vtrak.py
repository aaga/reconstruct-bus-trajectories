"""Dense VTRAK/ROCKET location list → canonical trace.

ROCKET is a raw per-vehicle feed (uppercase columns, naive Chicago-local times,
no GTFS trip identity), so this adapter maps the position columns and takes the
route/pattern/trip identity from the spec. ``dataio.vtrak.load_rocket_csv`` does
the read + UTC conversion (``ts_utc``); here we just reshape.

spec: {"kind": "vtrak", "path", "veh_id", "route_id", "pattern_id", "trip_id"?,
       "lat_col"?="LAT", "lon_col"?="LON"}
"""

from __future__ import annotations

import pandas as pd

from dataio.vtrak import load_rocket_csv


def load(spec: dict) -> pd.DataFrame:
    veh = str(spec["veh_id"])
    raw = load_rocket_csv(spec["path"], veh_ids=[veh])
    lat_col = spec.get("lat_col", "LAT")
    lon_col = spec.get("lon_col", "LON")
    return pd.DataFrame({
        "avl_event_time": raw["ts_utc"].dt.tz_convert(None),
        "latitude": raw[lat_col].astype(float),
        "longitude": raw[lon_col].astype(float),
        "trip_id": str(spec.get("trip_id", veh)),
        "bus_id": raw["VEH_ID"].astype(str),
        "route_id": str(spec["route_id"]),
        "pattern_id": str(spec["pattern_id"]),
    })
