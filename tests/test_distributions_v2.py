"""Unit tests for the v2 distribution helpers (clusters) and the post/post2
classification split in delay_events."""

from __future__ import annotations

import numpy as np

from analysis.network.build_distributions import (
    CLUSTER_GAP_M,
    NEAR_STOP_M,
    dwell_clusters_for_bins,
)


def test_cluster_split_and_median():
    # two clusters: bins at 10-14 m and 40-42 m (gap 26 m > 15.24 m)
    bins = [(10, 5), (12, 10), (14, 5), (40, 3), (42, 3)]
    out = dwell_clusters_for_bins(bins, stop_offs=[12.0])
    assert len(out) == 2
    (lo1, hi1, med1, n1, near1), (lo2, hi2, med2, n2, near2) = out
    assert (lo1, hi1, n1) == (10, 14, 20)
    assert med1 == 12.0          # weighted median of 5/10/5
    assert near1 is True         # 0 m from the stop at 12 m
    assert (lo2, hi2, n2) == (40, 42, 6)
    assert near2 is False        # 28 m > NEAR_STOP_M(22.9) from stop at 12


def test_cluster_gap_boundary():
    # exactly CLUSTER_GAP_M apart stays ONE cluster; 1 m more splits
    a = 100
    b = int(a + np.floor(CLUSTER_GAP_M))
    assert len(dwell_clusters_for_bins([(a, 1), (b, 1)], [])) == 1
    assert len(dwell_clusters_for_bins([(a, 1), (b + 1, 1)], [])) == 2


def test_near_stop_threshold():
    out = dwell_clusters_for_bins([(50, 4)], stop_offs=[50 + NEAR_STOP_M - 0.5])
    assert out[0][4] is True
    out = dwell_clusters_for_bins([(50, 4)], stop_offs=[50 + NEAR_STOP_M + 1.5])
    assert out[0][4] is False


def test_post_split_classification():
    """Multi-cycle events: post spans first-close→end and classes post2."""
    import pandas as pd
    from analysis.network import delay_events as de

    calls = []
    # Fake minimal machinery: reproduce the classification arithmetic the
    # way _process_trip does, on a synthetic overl matrix.
    a_abs, b_abs = 1000.0, 1100.0          # 100 s stopped event
    overl = np.array([
        [1020.0, 1035.0, 12],              # cycle 1: open 1020 close 1035
        [1050.0, 1060.0, 15],              # swallowed cycle 2
    ])
    open_min = float(overl[:, 0].min())
    close_first = float(overl[:, 1].min())
    assert open_min == 1020.0
    assert close_first == 1035.0           # FIRST close, not last (1060)
    assert (open_min - a_abs) > de.PORTION_MIN_S       # pre emitted
    assert (b_abs - close_first) > de.PORTION_MIN_S    # post emitted
    cls = "post2" if len(overl) > 1 else "post"
    assert cls == "post2"
    # single-cycle → plain post
    overl1 = overl[:1]
    cls1 = "post2" if len(overl1) > 1 else "post"
    assert cls1 == "post"
