"""Unit tests for the LOCREG-PCHIP smoother."""

from __future__ import annotations

import numpy as np

from bus_trajectories.smooth import enforce_monotonic, locreg, locreg_pchip


def test_enforce_monotonic_forward_fills_decreases():
    x = np.array([0.0, 1.0, 0.5, 2.0, 1.5, 3.0])
    expected = np.array([0.0, 1.0, 1.0, 2.0, 2.0, 3.0])
    np.testing.assert_array_equal(enforce_monotonic(x), expected)


def test_locreg_pchip_recovers_quadratic_under_noise():
    rng = np.random.default_rng(0)
    t = np.linspace(0, 100, 200)
    truth = 0.5 * t**2  # strictly monotone increasing on t >= 0
    noisy = truth + rng.normal(0, 5.0, size=t.size)
    res = locreg_pchip(t, noisy, bandwidth=20, degree=3)
    rmse = float(np.sqrt(np.mean((res.f(t) - truth) ** 2)))
    assert rmse < 5.0, f"smoothed RMSE too large: {rmse}"


def test_locreg_pchip_is_monotonic_even_with_backwards_spike():
    t = np.linspace(0, 50, 100)
    d = np.where((t > 25) & (t < 26), -50.0, t)  # one violent backwards spike
    res = locreg_pchip(t, d, bandwidth=20, degree=3)
    grid = np.linspace(t[0], t[-1], 1000)
    f_grid = res.f(grid)
    diffs = np.diff(f_grid)
    assert np.all(diffs >= -1e-9), f"non-monotonic samples: {(diffs < -1e-9).sum()}"


def test_locreg_pchip_speed_nonnegative():
    rng = np.random.default_rng(1)
    t = np.linspace(0, 200, 300)
    d = np.cumsum(np.abs(rng.normal(2.0, 0.5, size=t.size)))  # noisy positive increments
    res = locreg_pchip(t, d, bandwidth=20, degree=3)
    grid = np.linspace(t[0], t[-1], 1000)
    v = res.f.derivative()(grid)
    assert np.all(v >= -1e-9), f"negative speed at {(v < -1e-9).sum()} samples"


def test_locreg_handles_short_input():
    t = np.array([0.0, 1.0, 2.0, 3.0])
    d = np.array([0.0, 1.0, 4.0, 9.0])
    out = locreg(t, d, bandwidth=20, degree=3)
    assert out.shape == d.shape
    # Trajectory roughly recovered (relaxed tolerance for short input).
    assert np.all(np.abs(out - d) < 1.0)
