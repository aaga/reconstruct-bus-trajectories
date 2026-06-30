"""Unit tests for the restored LOCREG-MQSI smoother.

MQSI is the C^2 counterpart of LOCREG-PCHIP: monotone with *continuous*
acceleration. These tests lock in the three properties build_pchip_vs_mqsi
relies on — interpolation, monotonicity, and C^2 continuity — and contrast the
last against PCHIP's C^1 (jumping acceleration).
"""

from __future__ import annotations

import numpy as np

from bus_trajectories.smooth import locreg_mqsi, locreg_pchip


def _monotone_trip():
    t = np.arange(0.0, 40.0, 1.0)
    # strictly increasing distance-into-trip with varying speed
    d = np.cumsum(np.abs(np.sin(t / 5.0)) * 3.0 + 1.0)
    return t, d


def test_mqsi_is_monotone_non_decreasing():
    t, d = _monotone_trip()
    f = locreg_mqsi(t, d, bandwidth=10).f
    grid = np.linspace(t.min(), t.max(), 2000)
    diffs = np.diff(f(grid))
    assert np.all(diffs >= -1e-6)


def test_mqsi_has_continuous_acceleration():
    """f'' is continuous across a knot (C^2), unlike PCHIP which jumps (C^1)."""
    t, d = _monotone_trip()
    knot = 20.0
    a2 = locreg_mqsi(t, d, bandwidth=10).f.derivative(2)
    left, right = float(a2(knot - 1e-4)), float(a2(knot + 1e-4))
    assert abs(left - right) < 1e-2

    # PCHIP, by contrast, has a discontinuous second derivative at the knot.
    p2 = locreg_pchip(t, d, bandwidth=10).f.derivative(2)
    pl, pr = float(p2(knot - 1e-4)), float(p2(knot + 1e-4))
    assert abs(pl - pr) > 1e-2


def test_mqsi_interpolates_smoothed_knots():
    """f(t_i) matches the LOCREG-smoothed, monotonized knot value x_i."""
    t, d = _monotone_trip()
    res = locreg_mqsi(t, d, bandwidth=10)
    interior = slice(1, -1)
    np.testing.assert_allclose(res.f(t[interior]), res.x[interior], atol=1e-6)


def test_mqsi_derivatives_are_callable_arrays():
    t, d = _monotone_trip()
    f = locreg_mqsi(t, d, bandwidth=10).f
    grid = np.linspace(t.min(), t.max(), 100)
    assert f.derivative(1)(grid).shape == grid.shape
    assert f.derivative(2)(grid).shape == grid.shape
