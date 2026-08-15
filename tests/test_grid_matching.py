"""Grid-accelerated matching: snap parity (step B) + preselection (step A).

The per-shape segment grid must be invisible: match() bitwise-equals the
full-scan match_brute() on every output array, including far-off-shape pings
(brute fallback) and degenerate zero-length segments. The CandidateGrid
preselection must reproduce the exact scorer's decisions on
forward/reverse/short-turn/off-route trips.
"""

from __future__ import annotations

import numpy as np
import pytest

from core.mapmatch.shape_snap import SnapToShapeMatcher
from analysis.network.assign_shapes import (
    CandidateGrid,
    _choose_shape_exact,
    choose_shape,
)


def _wiggly_shape(rng, nv, lat0=41.8, lon0=-87.7):
    lat = lat0 + np.cumsum(np.abs(rng.normal(1e-4, 1e-4, nv)))
    lon = lon0 + np.cumsum(rng.normal(0, 2e-4, nv))
    return np.column_stack([lat, lon])


def test_grid_snap_bitwise_equals_brute():
    rng = np.random.default_rng(11)
    for case in range(25):
        nv = int(rng.integers(5, 2000))
        poly = _wiggly_shape(rng, nv)
        if case % 3 == 0:  # degenerate zero-length segments
            for idx in rng.integers(1, nv - 1, int(rng.integers(1, 4))):
                poly[idx] = poly[idx - 1]
        m = SnapToShapeMatcher(poly, max_perp_m=50.0)
        n = int(rng.integers(1, 300))
        # mix of near-shape and far-away pings (exercises the brute fallback)
        base = poly[rng.integers(0, nv, n)]
        noise = rng.normal(0, 3e-4, (n, 2))
        far = rng.random(n) < 0.15
        noise[far] += rng.normal(0, 0.05, (int(far.sum()), 2))
        plat, plon = base[:, 0] + noise[:, 0], base[:, 1] + noise[:, 1]

        g = m.match(plat, plon)
        b = m.match_brute(plat, plon)
        assert np.array_equal(g.on_route, b.on_route)
        assert np.array_equal(g.segment_idx, b.segment_idx)
        assert np.array_equal(g.frac, b.frac)
        assert np.array_equal(g.dist_along_m, b.dist_along_m)
        assert np.array_equal(g.perp_dist_m, b.perp_dist_m)


def _route_candidates():
    """Forward full shape, its reverse, and a short-turn prefix."""
    nv = 400
    lat = 41.80 + np.arange(nv) * 1.5e-4          # ~16.7 m per vertex, ~6.6 km
    lon = np.full(nv, -87.70)
    fwd = np.column_stack([lat, lon])
    rev = fwd[::-1].copy()
    short = fwd[: nv // 2].copy()
    matchers = {
        "fwd": SnapToShapeMatcher(fwd),
        "rev": SnapToShapeMatcher(rev),
        "short": SnapToShapeMatcher(short),
    }
    lens = {k: v.total_length_m for k, v in matchers.items()}
    return matchers, lens


@pytest.mark.parametrize(
    "kind", ["full_forward", "short_turn", "reverse", "off_route"]
)
def test_grid_selection_matches_exact(kind):
    matchers, lens = _route_candidates()
    rng = np.random.default_rng(3)
    n = 120
    if kind == "full_forward":
        lat = 41.80 + np.linspace(0, 399, n) * 1.5e-4
        lon = np.full(n, -87.70)
    elif kind == "short_turn":
        lat = 41.80 + np.linspace(0, 190, n) * 1.5e-4
        lon = np.full(n, -87.70)
    elif kind == "reverse":
        lat = 41.80 + np.linspace(399, 0, n) * 1.5e-4
        lon = np.full(n, -87.70)
    else:  # parallel street 500 m away
        lat = 41.80 + np.linspace(0, 399, n) * 1.5e-4
        lon = np.full(n, -87.694)
    lat = lat + rng.normal(0, 5e-5, n)
    lon = lon + rng.normal(0, 5e-5, n)

    exact = _choose_shape_exact(lat, lon, matchers, lens)
    grid = choose_shape(lat, lon, matchers, lens)
    if isinstance(exact, str):
        assert grid == exact
    else:
        assert not isinstance(grid, str)
        assert grid.shape_id == exact.shape_id
        assert grid.score == pytest.approx(exact.score)
        assert np.array_equal(grid.match.dist_along_m, exact.match.dist_along_m)


def test_candidate_grid_tally_direction():
    matchers, _ = _route_candidates()
    g = CandidateGrid(matchers)
    n = 100
    lat = 41.80 + np.linspace(0, 399, n) * 1.5e-4
    lon = np.full(n, -87.70)
    approx = g.approx_scores(lat, lon)
    # forward shape must strongly out-tally its reverse on a forward trip
    assert approx["fwd"][0] > 0.8
    assert approx.get("rev", (0.0, 0))[0] < 0.2


def test_grid_snap_exact_far_false_on_route_parity():
    """exact_far=False: on_route mask and all on-route rows still bitwise
    match the brute reference; only beyond-threshold rows may differ."""
    rng = np.random.default_rng(23)
    for _ in range(15):
        nv = int(rng.integers(10, 1500))
        poly = _wiggly_shape(rng, nv)
        m = SnapToShapeMatcher(poly, max_perp_m=50.0)
        n = int(rng.integers(5, 300))
        base = poly[rng.integers(0, nv, n)]
        noise = rng.normal(0, 3e-4, (n, 2))
        far = rng.random(n) < 0.5  # half the pings way off-shape
        noise[far] += rng.normal(0, 0.05, (int(far.sum()), 2))
        plat, plon = base[:, 0] + noise[:, 0], base[:, 1] + noise[:, 1]

        g = m.match(plat, plon, exact_far=False)
        b = m.match_brute(plat, plon)
        assert np.array_equal(g.on_route, b.on_route)
        on = b.on_route
        assert np.array_equal(g.dist_along_m[on], b.dist_along_m[on])
        assert np.array_equal(g.perp_dist_m[on], b.perp_dist_m[on])
        assert np.array_equal(g.segment_idx[on], b.segment_idx[on])
