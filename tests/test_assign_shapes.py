"""Geometric trip→shape assignment on synthetic two-direction + short-turn shapes."""

from __future__ import annotations

import numpy as np

from analysis.network.assign_shapes import Assignment, choose_shape, monotone_frac
from core.mapmatch.shape_snap import SnapToShapeMatcher


def _straight_shape(n=100, lat0=41.80, dlat=0.10, reverse=False):
    """A due-north (or south) straight polyline ~11 km long."""
    lats = np.linspace(lat0, lat0 + dlat, n)
    if reverse:
        lats = lats[::-1]
    lons = np.full(n, -87.65)
    return np.column_stack([lats, lons])


def _pings_along(shape, idxs, jitter=0.00005, seed=1):
    rng = np.random.default_rng(seed)
    pts = shape[idxs].copy()
    pts += rng.normal(0, jitter, pts.shape)
    return pts[:, 0], pts[:, 1]


def _matchers(shapes):
    out, lens = {}, {}
    for sid, poly in shapes.items():
        m = SnapToShapeMatcher(poly)
        out[sid] = m
        lens[sid] = float(m._cum_at_vert[-1])
    return out, lens


def test_monotone_frac_forward_and_backward():
    assert monotone_frac(np.array([0.0, 100.0, 200.0, 300.0])) == 1.0
    assert monotone_frac(np.array([300.0, 200.0, 100.0, 0.0])) == 0.0
    # jitter below the 30 m floor is ignored
    assert monotone_frac(np.array([0.0, 5.0, 4.0, 6.0])) == 0.0  # no big moves


def test_correct_direction_wins():
    nb = _straight_shape()
    sb = _straight_shape(reverse=True)
    matchers, lens = _matchers({"nb": nb, "sb": sb})
    lats, lons = _pings_along(nb, np.arange(5, 95, 3))
    got = choose_shape(lats, lons, matchers, lens)
    assert isinstance(got, Assignment)
    assert got.shape_id == "nb"
    assert got.frac_monotone > 0.95


def test_short_turn_prefers_short_shape():
    full = _straight_shape(n=100, dlat=0.10)
    short = full[:55]  # same street, first ~55%
    matchers, lens = _matchers({"full": full, "short": short})
    # Trip covers only the shared portion — scores tie; shorter shape must win.
    lats, lons = _pings_along(full, np.arange(2, 50, 2))
    got = choose_shape(lats, lons, matchers, lens)
    assert isinstance(got, Assignment)
    assert got.shape_id == "short"


def test_full_length_trip_beats_short_shape():
    full = _straight_shape(n=100, dlat=0.10)
    short = full[:55]
    matchers, lens = _matchers({"full": full, "short": short})
    lats, lons = _pings_along(full, np.arange(2, 98, 2))
    got = choose_shape(lats, lons, matchers, lens)
    assert isinstance(got, Assignment)
    assert got.shape_id == "full"


def test_off_route_trip_rejected():
    nb = _straight_shape()
    matchers, lens = _matchers({"nb": nb})
    lats = np.linspace(41.80, 41.90, 30)
    lons = np.full(30, -87.20)  # ~35 km east — nowhere near the shape
    got = choose_shape(lats, lons, matchers, lens)
    assert got == "few_on_route"


def test_no_candidates():
    assert choose_shape(np.array([41.8]), np.array([-87.65]), {}, {}) == "no_candidates"
