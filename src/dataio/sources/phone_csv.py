"""Phone-GPS CSV → canonical trace.

The Record-a-Ride export (``pings_<trip_id>.csv``) is already written in
``load_avl_csv``'s column shape, so this is a thin wrapper: load, optionally
filter to one route/pattern, and (if the CSV predates pattern tagging) stamp a
``pattern_id`` from the spec.

spec: {"kind": "phone_csv", "path": ..., "route_id"?: ..., "pattern_id"?: ...}
"""

from __future__ import annotations

import pandas as pd

from dataio.gtfs import load_avl_csv


def load(spec: dict) -> pd.DataFrame:
    df = load_avl_csv(spec["path"], drop_deadhead=spec.get("drop_deadhead", True))
    if "pattern_id" not in df.columns and spec.get("pattern_id"):
        df["pattern_id"] = str(spec["pattern_id"])
    if spec.get("route_id") is not None:
        df = df[df["route_id"].astype(str) == str(spec["route_id"])].copy()
    if spec.get("pattern_id") is not None:
        df = df[df["pattern_id"].astype(str) == str(spec["pattern_id"])].copy()
    return df
