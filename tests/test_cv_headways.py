"""Headway set fixed per segment (delay_events.cv_headway_rows)."""
import sys
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from analysis.network.delay_events import cv_headway_rows  # noqa: E402

TZ = "America/New_York"
T0 = pd.Timestamp("2026-10-22 12:00", tz=TZ).timestamp()


def _trip(t_up, dur=60.0, nb=3):
    # bucket nb-1 is upstream (crossed first), bucket 0 downstream (last)
    return {b: t_up + dur * (nb - 1 - b) / (nb - 1) for b in range(nb)}


def _by_bucket(rows):
    return {r["bucket"]: r for r in rows}


def test_gap_on_cap_counts_the_same_at_every_bucket():
    # 3,599.9 s apart upstream, stretching past 3,600 s downstream: the
    # second bus is slower. Old per-bucket rule dropped it downstream only.
    trips = [_trip(T0, dur=60), _trip(T0 + 3599.9, dur=61)]
    rows = _by_bucket(cv_headway_rows({"s": trips}, 7, 19, TZ))
    assert {b: r["n"] for b, r in rows.items()} == {0: 1, 1: 1, 2: 1}


def test_gap_over_cap_upstream_is_a_break_everywhere():
    trips = [_trip(T0), _trip(T0 + 3600.5, dur=50), _trip(T0 + 4800)]
    rows = _by_bucket(cv_headway_rows({"s": trips}, 7, 19, TZ))
    assert all(r["n"] == 1 for r in rows.values())
    assert all(abs(r["sum_h"] - 1199.5) < 11 for r in rows.values())


def test_window_edge_decided_at_upstream_end():
    seven = pd.Timestamp("2026-10-22 07:00", tz=TZ).timestamp()
    # crosses upstream at 06:59:50 (out), downstream after 07:00 (would be in)
    trips = [_trip(seven - 10, dur=60), _trip(seven + 600), _trip(seven + 1200)]
    rows = _by_bucket(cv_headway_rows({"s": trips}, 7, 19, TZ))
    assert all(r["n"] == 1 for r in rows.values())


def test_overtaking_stays_positive():
    a, b = _trip(T0, dur=200), _trip(T0 + 30, dur=60)  # b passes a
    rows = _by_bucket(cv_headway_rows({"s": [a, b]}, 7, 19, TZ))
    assert all(r["n"] == 1 and r["sum_h"] > 0 for r in rows.values())
