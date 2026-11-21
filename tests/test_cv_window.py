"""Headway-CV window selection rule (cv_window.qualifying / scan)."""
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from analysis.network.cv_window import qualifying, scan  # noqa: E402


def _dirs(per_hour_0, per_hour_1):
    return {"0": Counter(per_hour_0), "1": Counter(per_hour_1)}


def test_both_directions_every_hour():
    all_day = {h: 6 for h in range(6, 22)}
    peak_only = {h: (6 if h in (7, 8, 16, 17) else 2) for h in range(6, 22)}
    hd = {
        "A": _dirs(all_day, all_day),      # frequent all day
        "B": _dirs(all_day, peak_only),    # one direction thin midday
        "C": _dirs(peak_only, peak_only),  # peak-only frequent
        "D": {"0": Counter(all_day)},      # one-direction route: skipped
    }
    assert qualifying(hd, 10, 15) == ["A"]
    assert qualifying(hd, 16, 18) == ["A", "B", "C"]  # B frequent at peak


def test_exactly_four_per_hour_counts_as_15_min():
    hd = {"A": _dirs({10: 4, 11: 4}, {10: 4, 11: 4})}
    assert qualifying(hd, 10, 12) == ["A"]
    hd["A"]["1"][11] = 3
    assert qualifying(hd, 10, 12) == []


def test_scan_covers_windows_of_two_hours_or_more_inside_8_18():
    res = scan({})
    assert all(8 <= a < b <= 18 and b - a >= 2 for a, b, _ in res)
    assert (8, 18, 0) in res and (16, 18, 0) in res
