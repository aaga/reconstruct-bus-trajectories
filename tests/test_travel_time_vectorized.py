"""last_times_at_boundaries must agree with the scalar _last_t_at_x."""

from __future__ import annotations

import numpy as np
from scipy.interpolate import PchipInterpolator

from core.decompose.travel_time import _last_t_at_x, last_times_at_boundaries


def _trip_with_dwell():
    """Monotone trajectory: cruise, 60 s dwell at x=500, cruise to 1200 m."""
    t = np.array([0.0, 50.0, 100.0, 160.0, 220.0, 300.0])
    x = np.array([0.0, 250.0, 500.0, 500.0, 800.0, 1200.0])
    return PchipInterpolator(t, x)


def test_matches_scalar_on_interior_boundaries():
    f = _trip_with_dwell()
    xs = np.array([100.0, 250.0, 499.0, 500.0, 799.0, 1100.0])
    got = last_times_at_boundaries(f, xs, n_grid=4000)
    want = np.array([_last_t_at_x(f, x) for x in xs])
    np.testing.assert_allclose(got, want, atol=1e-6)


def test_dwell_uses_right_endpoint():
    f = _trip_with_dwell()
    (t_at_dwell,) = last_times_at_boundaries(f, np.array([500.0]), n_grid=4000)
    # The bus sits at x=500 from t=100 to t=160; "last time at x" ≈ 160.
    assert 155.0 < t_at_dwell <= 165.0


def test_clips_outside_range():
    f = _trip_with_dwell()
    got = last_times_at_boundaries(f, np.array([-10.0, 5000.0]), n_grid=4000)
    assert got[0] == float(f.x[0])  # already past x at start -> clip to t_lo
    assert got[1] == float(f.x[-1])  # never reaches -> clip to t_hi


def test_unsorted_boundaries_align_with_input_order():
    f = _trip_with_dwell()
    xs = np.array([1100.0, 100.0, 500.0])
    got = last_times_at_boundaries(f, xs, n_grid=4000)
    want = np.array([_last_t_at_x(f, x) for x in xs])
    np.testing.assert_allclose(got, want, atol=1e-6)
