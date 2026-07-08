"""Unit tests for the snap-to-shape map matcher."""

from __future__ import annotations

import math

import numpy as np
import pytest

from core.mapmatch import get_matcher
from core.mapmatch.shape_snap import SnapToShapeMatcher
from core.mapmatch.valhalla import ValhallaMatcher


def _straight_line_shape(start_lat=41.9, start_lon=-87.65, n=11, length_m=1000.0):
    """Build an east-west polyline of ``length_m`` total in ``n`` evenly spaced points."""
    lat0 = start_lat
    mlon = 111320.0 * math.cos(math.radians(lat0))
    dlon_per_m = 1.0 / mlon
    lons = start_lon + np.linspace(0, length_m, n) * dlon_per_m
    lats = np.full(n, start_lat)
    return np.column_stack([lats, lons])


def test_snap_to_straight_line_zero_offset():
    shape = _straight_line_shape(length_m=1000.0, n=11)
    m = SnapToShapeMatcher(shape, max_perp_m=10.0)
    # Pick three pings exactly on the line at known fractions.
    test_lats = np.array([shape[0, 0], shape[5, 0], shape[10, 0]])
    test_lons = np.array([shape[0, 1], shape[5, 1], shape[10, 1]])
    res = m.match(test_lats, test_lons)
    assert res.on_route.all()
    np.testing.assert_allclose(res.dist_along_m, [0.0, 500.0, 1000.0], atol=0.5)
    np.testing.assert_array_less(res.perp_dist_m, 0.5)


def test_snap_perp_distance_offroute_flag():
    shape = _straight_line_shape(length_m=1000.0, n=11)
    m = SnapToShapeMatcher(shape, max_perp_m=20.0)
    # Move one ping ~100 m perpendicular (north) from the line.
    lat = shape[5, 0] + 100.0 / 111320.0
    lon = shape[5, 1]
    res = m.match(np.array([lat]), np.array([lon]))
    assert res.on_route[0] == False  # noqa: E712
    assert 95 < res.perp_dist_m[0] < 105
    np.testing.assert_allclose(res.dist_along_m[0], 500.0, atol=0.5)


def test_snap_clamps_to_polyline_endpoints():
    shape = _straight_line_shape(length_m=1000.0, n=11)
    m = SnapToShapeMatcher(shape, max_perp_m=200.0)
    # Ping 200 m before start: should snap to vertex 0, dist_along ~ 0.
    mlon = 111320.0 * math.cos(math.radians(shape[0, 0]))
    lon = shape[0, 1] - 200.0 / mlon
    lat = shape[0, 0]
    res = m.match(np.array([lat]), np.array([lon]))
    np.testing.assert_allclose(res.dist_along_m[0], 0.0, atol=0.5)
    assert 195 < res.perp_dist_m[0] < 205


def test_get_matcher_factory():
    shape = _straight_line_shape()
    sn = get_matcher("shape_snap", polyline_latlon=shape)
    assert isinstance(sn, SnapToShapeMatcher)
    val = get_matcher("valhalla")
    assert isinstance(val, ValhallaMatcher)
    with pytest.raises(ValueError):
        get_matcher("nope")


def test_valhalla_stub_raises():
    m = ValhallaMatcher()
    with pytest.raises(NotImplementedError):
        m.match(np.array([41.9]), np.array([-87.65]))
