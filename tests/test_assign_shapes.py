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


def test_deadhead_head_trimmed_and_accepted():
    """A clean revenue trip preceded by a long off-route pull-out (next
    trip's id assigned during dead-head) must be accepted: scoring trims
    to the first..last on-route ping."""
    full = _straight_shape()
    shapes = {"full": full}
    m, lens = _matchers(shapes)
    # 60 revenue pings along the route + 40-ping dead-head ~2 km east
    lat_r, lon_r = _pings_along(full, np.linspace(0, 99, 60).astype(int))
    lat_d = np.full(40, 41.79)
    lon_d = np.linspace(-87.625, -87.648, 40)  # approaching from off-route
    lats = np.concatenate([lat_d, lat_r])
    lons = np.concatenate([lon_d, lon_r])
    got = choose_shape(lats, lons, m, lens)
    assert isinstance(got, Assignment)
    assert got.shape_id == "full"
    assert got.score > 0.9  # trimmed window is clean
    # off-route head still excluded from the on-route mask downstream
    assert got.match.on_route[:40].sum() == 0


def test_full_trip_not_overtrimmed_to_subshape():
    """A trip covering the FULL route must match the full shape, not get
    trimmed down to a perfect-looking sub-shape (explicit 2026-07-30 spec)."""
    full = _straight_shape()
    short = full[:50]  # first half
    shapes = {"full": full, "short": short}
    m, lens = _matchers(shapes)
    lats, lons = _pings_along(full, np.linspace(0, 99, 80).astype(int))
    got = choose_shape(lats, lons, m, lens)
    assert isinstance(got, Assignment)
    assert got.shape_id == "full"
    # and with a dead-head head too
    lats2 = np.concatenate([np.full(30, 41.79), lats])
    lons2 = np.concatenate([np.linspace(-87.62, -87.649, 30), lons])
    got2 = choose_shape(lats2, lons2, m, lens)
    assert isinstance(got2, Assignment)
    assert got2.shape_id == "full"


def test_short_turn_still_prefers_short_shape_with_trimming():
    """A genuine short-turn trip (covers only the sub-shape's extent) still
    lands on the short variant — trimming must not break the tie-break."""
    full = _straight_shape()
    short = full[:50]
    shapes = {"full": full, "short": short}
    m, lens = _matchers(shapes)
    lats, lons = _pings_along(full, np.linspace(0, 48, 40).astype(int))
    got = choose_shape(lats, lons, m, lens)
    assert isinstance(got, Assignment)
    assert got.shape_id == "short"


def test_wandering_trip_still_rejected_with_trimming():
    """Off-route wandering BETWEEN two on-route touches is interior — the
    trim window keeps it, so the trip still fails the gate."""
    full = _straight_shape()
    shapes = {"full": full}
    m, lens = _matchers(shapes)
    # touch the route at both ends, wander 2 km east in between
    lat_a, lon_a = _pings_along(full, np.arange(0, 8))
    lat_b, lon_b = _pings_along(full, np.arange(92, 100))
    lat_w = np.linspace(41.81, 41.89, 60)
    lon_w = np.full(60, -87.62)
    lats = np.concatenate([lat_a, lat_w, lat_b])
    lons = np.concatenate([lon_a, lon_w, lon_b])
    got = choose_shape(lats, lons, m, lens)
    assert got == "low_score"
