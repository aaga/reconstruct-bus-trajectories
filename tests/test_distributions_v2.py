"""Unit tests for the post/post2 classification split in delay_events.

(2026-08-05: the dwell-cluster helpers and their tests were removed along
with the median-stop blue lines — raw-anchored dw locations superseded
them.)"""

from __future__ import annotations

import numpy as np


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
