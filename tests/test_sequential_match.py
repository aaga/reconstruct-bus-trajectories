"""Loop-aware (sequential) shape matching — shape_snap._sequential_fix."""
import numpy as np

from analysis.network.assign_shapes import monotone_frac
from core.mapmatch.shape_snap import SnapToShapeMatcher

LON0, LAT0 = -87.65, 41.80
DLON_10M = 10 / (111320 * np.cos(np.radians(LAT0)))


def _out_and_back(n=60, dlat=0.02):
    """North 2.2 km on one street, then south on a parallel one 10 m away."""
    up = np.column_stack([np.linspace(LAT0, LAT0 + dlat, n), np.full(n, LON0)])
    top = np.array([[LAT0 + dlat, LON0 + DLON_10M]])
    down = np.column_stack([np.linspace(LAT0 + dlat, LAT0, n), np.full(n, LON0 + DLON_10M)])
    return np.vstack([up, top, down[1:]])


def _trip(shape, step=3, jitter_m=6.0, seed=0):
    rng = np.random.default_rng(seed)
    pts = shape[::step].copy()
    pts[:, 0] += rng.normal(0, jitter_m / 111320, len(pts))
    pts[:, 1] += rng.normal(0, jitter_m / (111320 * np.cos(np.radians(LAT0))), len(pts))
    return pts[:, 0], pts[:, 1]


def test_plain_matcher_breaks_on_out_and_back_but_sequential_does_not():
    shape = _out_and_back()
    lats, lons = _trip(shape)
    plain = SnapToShapeMatcher(shape).match(lats, lons, exact_far=False)
    seq_m = SnapToShapeMatcher(shape, sequential=True)
    assert seq_m.self_overlaps
    seq = seq_m.match(lats, lons, exact_far=False)
    assert monotone_frac(plain.dist_along_m[plain.on_route]) < 0.9
    assert monotone_frac(seq.dist_along_m[seq.on_route]) >= 0.95
    # the return leg ends near the far end of the shape, not near 0
    assert seq.dist_along_m[-1] > 0.9 * seq_m.total_length_m
    assert np.array_equal(plain.on_route, seq.on_route)


def test_non_overlapping_shape_is_bitwise_identical():
    shape = np.column_stack([np.linspace(LAT0, LAT0 + 0.05, 80),
                             LON0 + 0.01 * np.sin(np.linspace(0, 3, 80))])
    lats, lons = _trip(shape, step=2)
    a = SnapToShapeMatcher(shape).match(lats, lons, exact_far=False)
    m = SnapToShapeMatcher(shape, sequential=True)
    assert not m.self_overlaps
    b = m.match(lats, lons, exact_far=False)
    for f in ("segment_idx", "frac", "dist_along_m", "perp_dist_m", "on_route"):
        assert np.array_equal(getattr(a, f), getattr(b, f)), f


def test_single_pass_pings_keep_nearest_projection():
    shape = _out_and_back()
    # pings only on the southern half of the UP leg, far from the turn: each
    # has candidates on both legs (10 m apart) -> ambiguous, sequence picks up-leg
    lats = np.linspace(LAT0 + 0.001, LAT0 + 0.008, 20)
    lons = np.full(20, LON0 + 0.2 * DLON_10M)
    seq = SnapToShapeMatcher(shape, sequential=True).match(lats, lons, exact_far=False)
    assert (seq.dist_along_m < 0.5 * seq.dist_along_m.max() + 1500).all()
    assert monotone_frac(seq.dist_along_m) == 1.0


def test_sequential_off_per_call():
    shape = _out_and_back()
    lats, lons = _trip(shape)
    m = SnapToShapeMatcher(shape, sequential=True)
    a = m.match(lats, lons, exact_far=False, sequential=False)
    b = SnapToShapeMatcher(shape).match(lats, lons, exact_far=False)
    assert np.array_equal(a.dist_along_m, b.dist_along_m)
