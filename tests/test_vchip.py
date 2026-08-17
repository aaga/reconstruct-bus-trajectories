"""VCHIP-ME / plain-PCHIP reconstruction (UT-Austin comparative study)."""
import sys
from pathlib import Path

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from core.smooth import fit_trajectory, plain_pchip, vchip_me  # noqa: E402


def _grid(sm, n=2000):
    return np.linspace(sm.f.x[0], sm.f.x[-1], n)


def test_vchip_matches_positions_and_velocities():
    t = np.array([0.0, 20, 40, 60])
    d = np.array([0.0, 150, 320, 500])
    v = np.array([7.0, 8.0, 8.5, 9.0])
    sm = vchip_me(t, d, v)
    assert np.allclose(sm.f(t), d)
    # constraint inactive here (alpha, beta ~1) -> observed speeds honored
    assert np.allclose(sm.f.derivative()(t), v)


def test_vchip_monotone_even_with_hot_velocities():
    # absurd speeds vs displacement -> circle constraint must clamp
    t = np.array([0.0, 30, 60, 90])
    d = np.array([0.0, 10, 20, 30])
    v = np.array([30.0, 30.0, 30.0, 30.0])
    sm = vchip_me(t, d, v)
    x = sm.f(_grid(sm))
    assert (np.diff(x) >= -1e-9).all()


def test_vchip_dwell_interval_flat_and_zero_speed():
    t = np.array([0.0, 30, 60, 90])
    d = np.array([0.0, 200, 200, 400])
    v = np.array([9.0, 4.0, 3.0, 9.0])
    sm = vchip_me(t, d, v)
    g = np.linspace(31, 59, 50)
    assert np.allclose(sm.f(g), 200, atol=1e-6)          # holds still
    assert np.allclose(sm.f.derivative()(45.0), 0.0, atol=1e-9)


def test_vchip_nan_speeds_fall_back_to_secants():
    t = np.array([0.0, 30, 60, 90])
    d = np.array([0.0, 150, 300, 450])
    v = np.array([5.0, np.nan, np.nan, 5.0])
    sm = vchip_me(t, d, v)
    x = sm.f(_grid(sm))
    assert (np.diff(x) >= -1e-9).all()
    assert np.allclose(sm.f(t), d)


def test_vchip_negative_position_jitter_monotonized():
    t = np.array([0.0, 10, 20, 30, 40])
    d = np.array([0.0, 100, 95, 210, 300])   # GPS backstep
    v = np.array([10.0, 10, 10, 10, 10])
    sm = vchip_me(t, d, v)
    x = sm.f(_grid(sm))
    assert (np.diff(x) >= -1e-9).all()


def test_vchip_duplicate_timestamps_collapsed():
    t = np.array([0.0, 10, 10, 20])
    d = np.array([0.0, 90, 92, 200])
    v = np.array([9.0, 9, 9, 10])
    sm = vchip_me(t, d, v)
    assert sm.f.x.size == 3
    assert (np.diff(sm.f(_grid(sm))) >= -1e-9).all()


def test_plain_pchip_monotone_no_locreg():
    rng = np.random.default_rng(7)
    t = np.arange(0, 600, 20.0)
    d = np.linspace(0, 3000, t.size) + rng.normal(0, 8, t.size)
    sm = plain_pchip(t, d)
    x = sm.f(_grid(sm))
    assert (np.diff(x) >= -1e-9).all()
    # PCHIP passes through the monotonized observations exactly — no
    # LOCREG smoothing displacement at knots
    from core.smooth import enforce_monotonic
    assert np.allclose(sm.f(np.unique(t)), enforce_monotonic(d), atol=1e-9)


def test_fit_trajectory_routing():
    t = np.array([0.0, 30, 60])
    d = np.array([0.0, 200, 400])
    assert fit_trajectory(t, d, None).f.__class__.__name__ == "PchipInterpolator"
    v = np.array([7.0, 7.0, 7.0])
    assert fit_trajectory(t, d, v).f.__class__.__name__ == "CubicHermiteSpline"
    # mostly-missing speeds -> fallback
    v2 = np.array([7.0, np.nan, np.nan])
    assert fit_trajectory(t, d, v2).f.__class__.__name__ == "PchipInterpolator"


def test_vchip_requires_two_points():
    with pytest.raises(ValueError):
        vchip_me(np.array([1.0]), np.array([2.0]), np.array([3.0]))


def test_pattern_stop_reattribution_nearest():
    """Location-based door re-attribution picks the nearest pattern stop."""
    sys.path.insert(0, str(REPO))
    from analysis.network import delay_events as de
    de._G.clear()
    de._G["shapes"] = {"S1": {"seg_bounds": [["A", 0, 500], ["B", 500, 900]]}}
    de._G["seg_stops"] = {"A": [(100.0, "s1"), (350.0, "s2")],
                          "B": [(50.0, "s3")]}
    dists, ids = de._pattern_stops("S1")
    # A ends at 500: s1@400, s2@150; B ends at 900: s3@850
    assert list(dists) == [150.0, 400.0, 850.0]
    assert ids == ["s2", "s1", "s3"]
    import numpy as np
    for x, want in ((0, "s2"), (270, "s2"), (280, "s1"), (600, "s1"),
                    (630, "s3"), (2000, "s3")):
        j = np.searchsorted(dists, x)
        lo, hi = max(0, j - 1), min(len(dists) - 1, j)
        best = lo if abs(x - dists[lo]) <= abs(x - dists[hi]) else hi
        assert ids[best] == want, (x, ids[best])
    assert de._pattern_stops("missing") is None
    de._G.clear()
