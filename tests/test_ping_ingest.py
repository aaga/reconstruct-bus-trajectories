"""Timetable reversal and vehicle-run grouping (analysis/network/ping_ingest.py)."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "src"))

from analysis.network import ping_ingest as pi  # noqa: E402
from dataio.cities import get_city  # noqa: E402


class _TT:
    """Two trips on one 3 km shape: stops every 1000 m, 100 s apart, 20 s dwell at stop 2."""

    def __init__(self):
        d = np.array([0.0, 1000.0, 2000.0, 3000.0])
        arr = np.array([0.0, 100.0, 220.0, 320.0])
        dep = np.array([0.0, 120.0, 220.0, 320.0])
        self.dd = np.tile(np.repeat(d, 2), 2)
        self.tt = np.tile(np.c_[arr, dep].ravel(), 2)
        self.off = np.array([0, 4])
        self.n = np.array([4, 4])

    curve = pi.Timetable.curve


def test_reverse_steps_back_along_the_timetable():
    tt = _TT()
    # published at 500 m (schedule time 50 s), 20 s old -> real report at schedule 30 s = 300 m
    d = pi.reverse_positions(tt, np.array([0, 1]), np.array([500.0, 1500.0]), np.array([20.0, 20.0]))
    # second: 1500 m is schedule 170 s; 20 s earlier = 150 s = 1300 m
    assert np.allclose(d, [300.0, 1300.0])


def test_reverse_respects_scheduled_dwell_and_trip_start():
    tt = _TT()
    # 1100 m = 130 s; 20 s earlier = 110 s, inside the dwell at stop 2 -> the stop itself
    # 50 m = 5 s; 30 s earlier is before the trip starts -> held at the first stop
    d = pi.reverse_positions(tt, np.array([0, 0, 0]), np.array([1100.0, 50.0, 800.0]),
                             np.array([20.0, 30.0, 0.0]))
    assert np.allclose(d, [1000.0, 0.0, 800.0])   # zero age leaves the position alone


def _runs(rows):
    df = pd.DataFrame(rows, columns=["vehicle_id", "ts", "route_id", "direction_id", "trip_id",
                                     "d_m", "shape_len_m"])
    df["latitude"], df["longitude"], df["shape_key"] = 40.7, -74.0, -1
    return pi.assign_runs(get_city("nyc"), df)


def test_trip_id_flip_mid_route_stays_one_run():
    r = _runs([("v", 0, "M1", 0, "a", 1000, 9000), ("v", 30, "M1", 0, "a", 1200, 9000),
               ("v", 60, "M1", 0, "b", 1400, 9000), ("v", 90, "M1", 0, "a", 1600, 9000)])
    assert len(set(r)) == 1


def test_runs_split_on_gap_direction_vehicle_and_terminal():
    r = _runs([("v", 0, "M1", 0, "a", 1000, 9000), ("v", 30, "M1", 0, "a", 1200, 9000),
               ("v", 400, "M1", 0, "a", 3000, 9000),      # > 5 min silent
               ("v", 430, "M1", 1, "c", 3100, 9000),      # direction change
               ("v", 460, "M1", 1, "c", 8900, 9000),
               ("v", 490, "M1", 1, "d", 50, 9000),        # old trip finished, new one at its start
               ("w", 500, "M1", 1, "d", 60, 9000)])       # another vehicle
    assert list(r) == [1, 1, 2, 3, 3, 4, 5]
