"""Unit tests for the segment travel-time primitives.

``_last_t_at_x`` and ``segment_observed_time`` encode the chapter Eq 3.3
"last time at x" convention: dwell at a segment's downstream signal is
attributed to the segment that *ends* there. These were previously untested and
the right-endpoint-of-dwell rule is easy to get subtly wrong.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest
from scipy.interpolate import PchipInterpolator

# _last_t_at_x searches a 4000-point dense grid, so endpoints land within the
# grid spacing (~1e-3 over these trips) rather than exactly.
_GRID_TOL = 2e-3

from bus_trajectories.delay_decomposition.travel_time import (
    _last_t_at_x,
    segment_observed_time,
)


def _trip_with_dwell():
    """f(t): rises 0->10, dwells at x=10 over t in [1,3], then rises 10->20."""
    t = np.array([0.0, 1.0, 2.0, 3.0, 4.0])
    x = np.array([0.0, 10.0, 10.0, 10.0, 20.0])
    return PchipInterpolator(t, x)


def test_last_t_at_x_returns_right_end_of_dwell():
    f = _trip_with_dwell()
    # The bus sits at x=10 from t=1 to t=3; the "last time at x=10" is t=3.
    assert _last_t_at_x(f, 10.0) == pytest.approx(3.0, abs=_GRID_TOL)


def test_last_t_at_x_interior_value_is_monotone_inverse():
    f = _trip_with_dwell()
    # x=5 occurs once on the first ramp, between t=0 and t=1.
    t5 = _last_t_at_x(f, 5.0)
    assert 0.0 < t5 < 1.0
    assert abs(float(f(t5)) - 5.0) < 1e-3


def test_last_t_at_x_clips_out_of_range():
    f = _trip_with_dwell()
    assert _last_t_at_x(f, -1.0) == 0.0   # below start -> clip to t_lo
    assert _last_t_at_x(f, 99.0) == 4.0   # above end   -> clip to t_hi


def test_segment_observed_time_attributes_dwell_to_ending_segment():
    f = _trip_with_dwell()
    # Segment ending at the dwell distance (x=10) should absorb the full
    # 2 s dwell: T_obs = last_t_at(10) - last_t_at(0) = 3 - 0 = 3.
    seg = SimpleNamespace(x_start_m=0.0, x_end_m=10.0)
    assert segment_observed_time(f, seg) == pytest.approx(3.0, abs=_GRID_TOL)

    # The next segment (10 -> 20) starts at the *end* of the dwell, so it does
    # not double-count it: T_obs = last_t_at(20) - last_t_at(10) = 4 - 3 = 1.
    seg2 = SimpleNamespace(x_start_m=10.0, x_end_m=20.0)
    assert segment_observed_time(f, seg2) == pytest.approx(1.0, abs=_GRID_TOL)
