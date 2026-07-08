"""Unit tests for the shared route-geometry prep helpers."""

from __future__ import annotations

import numpy as np

from analysis.prep.geometry import bearing_from_polyline, cumulative_route_dist_m


def test_cumulative_route_dist_is_monotonic_and_starts_at_zero():
    # A short north-then-east dogleg near Chicago.
    poly = np.array([[41.90, -87.65], [41.91, -87.65], [41.91, -87.63]])
    cum = cumulative_route_dist_m(poly)
    assert cum[0] == 0.0
    assert np.all(np.diff(cum) > 0)
    # First leg is ~1 km of latitude (0.01 deg * 111320 m/deg).
    assert abs(cum[1] - 1113.2) < 1.0


def test_cumulative_route_dist_matches_equirectangular_formula():
    poly = np.array([[41.90, -87.65], [41.90, -87.64]])
    lat = np.radians(41.90)
    expected = 0.01 * 111320.0 * np.cos(lat)  # pure east step
    cum = cumulative_route_dist_m(poly)
    assert abs(cum[-1] - expected) < 1e-6


def test_cumulative_route_dist_rejects_bad_shape():
    try:
        cumulative_route_dist_m(np.zeros((3, 3)))
    except ValueError:
        return
    raise AssertionError("expected ValueError for non-(N,2) input")


def test_bearing_due_south_renders_left_to_right():
    # Motion due south (compass 180) → camera bearing 90 (screen-up = east).
    poly = np.array([[41.95, -87.65], [41.85, -87.65]])
    assert abs(bearing_from_polyline(poly) - 90.0) < 1e-6


def test_bearing_due_east_is_zero():
    # Motion due east (compass 90) → camera bearing 0.
    poly = np.array([[41.90, -87.66], [41.90, -87.64]])
    assert abs(bearing_from_polyline(poly) % 360.0) < 1e-6
