"""Pure route-polyline geometry shared by the dashboard data builders.

Single home for the camera-bearing and cumulative-distance helpers that were
once copy-pasted across the dashboard + comparison builders. Used today by
``analysis.route_aggregate`` and ``analysis.comparison``. No I/O, numpy only.
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


def slice_polyline(
    poly_latlon: np.ndarray,
    cumdist_m: np.ndarray,
    lo_m: float,
    hi_m: float,
) -> np.ndarray:
    """Return the ``(K, 2)`` lat/lon sub-polyline between route distances
    ``lo_m`` and ``hi_m``, with both endpoints interpolated exactly.

    ``cumdist_m`` is the per-vertex cumulative distance (GTFS
    ``shape_dist_traveled`` or :func:`cumulative_route_dist_m` fallback).
    Distances are clipped to the polyline's range; a degenerate span returns
    the two (identical) interpolated endpoints.
    """
    poly_latlon = np.asarray(poly_latlon, dtype=float)
    cumdist_m = np.asarray(cumdist_m, dtype=float)
    lo = float(np.clip(lo_m, cumdist_m[0], cumdist_m[-1]))
    hi = float(np.clip(hi_m, cumdist_m[0], cumdist_m[-1]))
    if hi < lo:
        lo, hi = hi, lo
    lat_lo = np.interp(lo, cumdist_m, poly_latlon[:, 0])
    lon_lo = np.interp(lo, cumdist_m, poly_latlon[:, 1])
    lat_hi = np.interp(hi, cumdist_m, poly_latlon[:, 0])
    lon_hi = np.interp(hi, cumdist_m, poly_latlon[:, 1])
    inside = (cumdist_m > lo) & (cumdist_m < hi)
    pts = [(lat_lo, lon_lo), *map(tuple, poly_latlon[inside]), (lat_hi, lon_hi)]
    return np.asarray(pts, dtype=float)


def simplify_polyline(poly_latlon: np.ndarray, tolerance_m: float = 5.0) -> np.ndarray:
    """Douglas-Peucker simplification with tolerance in meters.

    Works in a local equirectangular frame (fine at city scale). Always keeps
    the first and last vertices.
    """
    pts = np.asarray(poly_latlon, dtype=float)
    if len(pts) <= 2:
        return pts
    # Project to local meters around the mean latitude.
    mlat = np.cos(np.radians(pts[:, 0].mean()))
    xy = np.column_stack([pts[:, 1] * 111320.0 * mlat, pts[:, 0] * 111320.0])

    keep = np.zeros(len(pts), dtype=bool)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        i0, i1 = stack.pop()
        if i1 - i0 < 2:
            continue
        seg = xy[i1] - xy[i0]
        seg_len = np.hypot(*seg)
        rel = xy[i0 + 1 : i1] - xy[i0]
        if seg_len == 0.0:
            d = np.hypot(rel[:, 0], rel[:, 1])
        else:
            d = np.abs(rel[:, 0] * seg[1] - rel[:, 1] * seg[0]) / seg_len
        j = int(np.argmax(d))
        if d[j] > tolerance_m:
            k = i0 + 1 + j
            keep[k] = True
            stack.append((i0, k))
            stack.append((k, i1))
    return pts[keep]


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
