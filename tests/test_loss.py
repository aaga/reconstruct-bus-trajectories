"""Unit tests for accel/decel shoulder detection (loss.py).

``loss_shoulders_for_event`` finds the decel ramp before an event and the
accel ramp after it, bounded by a cruise-speed threshold. Pure array math,
previously untested.
"""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from bus_trajectories.delay_decomposition.loss import loss_shoulders_for_event


def _ramp_profile():
    """Speed (mph) over t: cruise 20 -> decel to 0 -> stop -> accel back to 20.

    Decel ramp 20->0 spans t in [5, 10] (crosses 12 mph near t=7).
    Stop (event) spans t in [10, 15].
    Accel ramp 0->20 spans t in [15, 20] (crosses 12 mph near t=18).
    """
    t = np.arange(0.0, 30.0, 0.5)
    v = np.full_like(t, 20.0)
    decel = (t >= 5) & (t <= 10)
    v[decel] = np.interp(t[decel], [5, 10], [20, 0])
    stop = (t > 10) & (t < 15)
    v[stop] = 0.0
    accel = (t >= 15) & (t <= 20)
    v[accel] = np.interp(t[accel], [15, 20], [0, 20])
    return t, v


def test_shoulders_bracket_the_event():
    t, v = _ramp_profile()
    event = SimpleNamespace(t_start=10.0, t_end=15.0)
    (decel_lo, decel_hi), (accel_lo, accel_hi) = loss_shoulders_for_event(
        t, v, event, cruise_threshold_mph=12.0
    )
    # The decel shoulder ends at t_start and begins on the ramp below cruise.
    assert decel_hi == 10.0
    assert 5.0 < decel_lo < 10.0
    # The accel shoulder begins at t_end and ends on the ramp below cruise.
    assert accel_lo == 15.0
    assert 15.0 < accel_hi < 20.0


def test_no_shoulders_when_cruise_never_reached():
    # Bus is always slow (below threshold) and flat: no transition ramps.
    t = np.arange(0.0, 20.0, 0.5)
    v = np.full_like(t, 3.0)
    event = SimpleNamespace(t_start=8.0, t_end=12.0)
    (decel_lo, decel_hi), (accel_lo, accel_hi) = loss_shoulders_for_event(
        t, v, event, cruise_threshold_mph=12.0
    )
    # With no decreasing/increasing ramp, shoulders collapse to the boundaries.
    assert decel_lo == decel_hi == 8.0
    assert accel_lo == accel_hi == 12.0


def test_empty_input_returns_degenerate_intervals():
    event = SimpleNamespace(t_start=4.0, t_end=9.0)
    (d, a) = loss_shoulders_for_event(
        np.array([]), np.array([]), event
    )
    assert d == (4.0, 4.0)
    assert a == (9.0, 9.0)
