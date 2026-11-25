"""Headway-CV end-to-end change (build_cv.delta_cv)."""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from analysis.network.build_cv import delta_cv  # noqa: E402


def test_bucket_zero_is_downstream():
    # CV grows toward the downstream signal (bucket 0): bunching builds
    cv = [0.70] * 5 + [0.60] * 20 + [0.50] * 5
    up, down, d = delta_cv(cv)
    assert (round(up, 2), round(down, 2), round(d, 2)) == (0.50, 0.70, 0.20)


def test_nulls_skipped_and_regularising_segment_negative():
    cv = [None, 0.4, 0.4, 0.4, 0.4, 0.4] + [0.5] * 10 + [0.6] * 5 + [None]
    up, down, d = delta_cv(cv)
    assert round(d, 2) == -0.20


def test_too_few_buckets():
    assert delta_cv([None, 0.5, None, 0.6]) is None
    assert delta_cv([]) is None


def test_short_segment_windows_do_not_overlap():
    cv = [0.8, 0.8, 0.8, 0.2, 0.2, 0.2]   # 6 valid buckets < 2 x 5
    up, down, d = delta_cv(cv)
    assert round(down, 2) == 0.8 and round(up, 2) == 0.2


def test_thin_coverage_buckets_ignored():
    # terminal fall-off: the downstream-most buckets saw few headways and a
    # biased (low) CV; they must not define the downstream end
    cv = [0.55, 0.60, 0.65] + [0.74] * 20 + [0.74] * 5
    n = [150, 400, 900] + [2270] * 25
    up, down, d = delta_cv(cv, n)
    assert round(down, 2) == 0.74 and abs(d) < 1e-9
    # without counts the artifact shows up as a spurious drop
    assert delta_cv(cv)[2] < -0.05
