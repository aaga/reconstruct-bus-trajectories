"""Pure route-polyline geometry shared by the dashboard data builders.

Single home for the camera-bearing and cumulative-distance helpers that were
previously copy-pasted across ``build_dashboard.py`` and ``build_comparison.py``
(the latter under the name ``_cumdist_geodesic``). No I/O, numpy only.
"""

from __future__ import annotations

import math

import numpy as np


def bearing_from_polyline(poly_latlon) -> float:
    """MapLibre camera bearing that makes start→end run left-to-right.

    MapLibre's bearing θ means "screen-up points to compass direction θ", so
    screen-right points to (θ + 90). For the bus's motion direction (the
    compass bearing of start→end on the polyline) to render screen-right, we
    need θ = motion_compass − 90.
    """
    lat0, lon0 = float(poly_latlon[0][0]), float(poly_latlon[0][1])
    lat1, lon1 = float(poly_latlon[-1][0]), float(poly_latlon[-1][1])
    mlat = math.cos(math.radians((lat0 + lat1) / 2))
    motion_compass = (
        math.degrees(math.atan2((lon1 - lon0) * mlat, lat1 - lat0)) + 360.0
    ) % 360.0
    return (motion_compass - 90.0 + 360.0) % 360.0


def cumulative_route_dist_m(poly_latlon) -> np.ndarray:
    """Equirectangular cumulative distance (m) along an ``(N, 2)`` lat/lon
    polyline. Used as the fallback when GTFS ``shape_dist_traveled`` is absent
    and to place features/segments along the route."""
    poly_latlon = np.asarray(poly_latlon, dtype=float)
    if poly_latlon.ndim != 2 or poly_latlon.shape[1] != 2:
        raise ValueError("poly_latlon must be (N, 2)")
    lat = poly_latlon[:, 0]
    lon = poly_latlon[:, 1]
    mlon_deg = 111320.0 * np.cos(np.radians((lat[:-1] + lat[1:]) / 2))
    dlat = (lat[1:] - lat[:-1]) * 111320.0
    dlon = (lon[1:] - lon[:-1]) * mlon_deg
    seg_m = np.hypot(dlat, dlon)
    out = np.zeros(len(lat))
    out[1:] = np.cumsum(seg_m)
    return out
