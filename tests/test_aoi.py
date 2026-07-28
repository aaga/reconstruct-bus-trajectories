"""AOI scoring: robust z, shrinkage, priority weighting."""

from __future__ import annotations

import numpy as np

from analysis.network.areas_of_interest import robust_z, shrunk


def test_robust_z_flags_outlier():
    vals = np.array([1.0, 1.1, 0.9, 1.05, 0.95, 1.0, 5.0])
    z = robust_z(vals)
    assert z[-1] > 10  # the 5.0 is wildly outside the MAD band
    assert np.abs(z[:-1]).max() < 2.5


def test_robust_z_constant_input():
    z = robust_z(np.full(5, 2.0))
    assert np.all(z == 0.0)


def test_shrinkage_monotone_in_n():
    z = np.array([3.0, 3.0, 3.0])
    n = np.array([5.0, 50.0, 5000.0])
    zs = shrunk(z, n)
    assert zs[0] < zs[1] < zs[2] < 3.0
    # Large n approaches the raw z.
    assert zs[2] > 2.95


def test_shrinkage_preserves_sign():
    zs = shrunk(np.array([-4.0]), np.array([10.0]))
    assert zs[0] < 0


def test_priority_weighting_prefers_heavy_service():
    # Same z*, different service intensity -> heavier segment ranks higher.
    z_star = 2.0
    light = z_star * (1.0 + 0.5 * np.log1p(1.0))
    heavy = z_star * (1.0 + 0.5 * np.log1p(20.0))
    assert heavy > light
