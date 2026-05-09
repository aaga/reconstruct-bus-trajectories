"""Snap-to-GTFS-shape map matcher.

For each ping, project (lat, lon) onto every segment of the shape polyline and
pick the nearest. Returns the cumulative distance to the projection point and
the perpendicular distance to the polyline.

This is a simplified stand-in for the Valhalla-based matching in
Huang et al. (ITSC 2023). It works because the GTFS shape *is* the predefined
route the paper validates against — so we collapse the two-step (snap to OSM
roads, then check whether segment is in route) into a single one-step
projection onto the route geometry directly.
"""

from __future__ import annotations

import math

import numpy as np

from . import MatchResult

_EARTH_R_M = 6371000.0
DEFAULT_MAX_PERP_M = 50.0  # paper uses Valhalla's internal threshold; 50m is a
# generous cushion that catches GPS noise on dense urban routes.


class SnapToShapeMatcher:
    """Snap pings to a GTFS shape polyline.

    By default, the cumulative distance along the polyline is computed from
    equirectangular segment lengths. If ``dist_along_m_per_vertex`` is given
    (e.g. from GTFS ``shape_dist_traveled``), those values are used instead —
    this is the GTFS-recommended approach and ensures ping distances line up
    with ``stop_times.shape_dist_traveled``-derived stop locations.
    """

    def __init__(
        self,
        polyline_latlon: np.ndarray,
        max_perp_m: float = DEFAULT_MAX_PERP_M,
        dist_along_m_per_vertex: np.ndarray | None = None,
    ):
        if polyline_latlon.ndim != 2 or polyline_latlon.shape[1] != 2:
            raise ValueError("polyline_latlon must have shape (N, 2) of (lat, lon)")
        if polyline_latlon.shape[0] < 2:
            raise ValueError("polyline must have at least 2 vertices")
        self.polyline = polyline_latlon
        self.max_perp_m = float(max_perp_m)

        # Local equirectangular projection around the polyline centroid.
        self._lat0 = float(polyline_latlon[:, 0].mean())
        self._lon0 = float(polyline_latlon[:, 1].mean())
        self._mlat = 111320.0
        self._mlon = 111320.0 * math.cos(math.radians(self._lat0))

        # Polyline vertices in meters: (N, 2) array of (x, y). Used for the
        # ping → segment matching step.
        self._verts = self._project(polyline_latlon[:, 0], polyline_latlon[:, 1])

        # Per-segment vector (b - a) and squared length (for projection geometry).
        a = self._verts[:-1]
        b = self._verts[1:]
        self._seg_a = a
        self._seg_v = b - a
        self._seg_len2 = (self._seg_v * self._seg_v).sum(axis=1)
        self._proj_seg_len = np.sqrt(self._seg_len2)

        # Cumulative distance along the polyline. Prefer GTFS-supplied values
        # when available; otherwise fall back to equirectangular cumulative.
        if dist_along_m_per_vertex is not None:
            cum = np.asarray(dist_along_m_per_vertex, dtype=float)
            if cum.shape[0] != polyline_latlon.shape[0]:
                raise ValueError(
                    "dist_along_m_per_vertex length must match polyline length"
                )
            self._cum_at_vert = cum
            self._seg_dist_len = np.diff(cum)
        else:
            cum = np.zeros(polyline_latlon.shape[0])
            cum[1:] = np.cumsum(self._proj_seg_len)
            self._cum_at_vert = cum
            self._seg_dist_len = np.diff(cum)
        self._cum_to_seg_start = self._cum_at_vert[:-1]
        self.total_length_m = float(self._cum_at_vert[-1])

    # ------------------------------------------------------------------ utils
    def _project(self, lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
        x = (np.asarray(lons, dtype=float) - self._lon0) * self._mlon
        y = (np.asarray(lats, dtype=float) - self._lat0) * self._mlat
        return np.column_stack([x, y])

    # ------------------------------------------------------------------ match
    def match(self, lats: np.ndarray, lons: np.ndarray) -> MatchResult:
        pts = self._project(lats, lons)  # (n, 2)
        n = pts.shape[0]
        m = self._seg_a.shape[0]

        # Vector from each segment start to each ping: shape (n, m, 2).
        # To keep memory in check we iterate per ping but vectorise across segments.
        seg_idx = np.empty(n, dtype=np.int64)
        frac = np.empty(n, dtype=float)
        dist_along = np.empty(n, dtype=float)
        perp = np.empty(n, dtype=float)

        a = self._seg_a  # (m, 2)
        v = self._seg_v  # (m, 2)
        seg_len2 = self._seg_len2  # (m,)
        seg_dist_len = self._seg_dist_len  # (m,) — meters of distance-along

        # Avoid division by zero on degenerate (zero-length) segments.
        safe_len2 = np.where(seg_len2 > 0, seg_len2, 1.0)

        for i in range(n):
            d = pts[i] - a  # (m, 2)
            t = (d * v).sum(axis=1) / safe_len2  # (m,)
            t = np.clip(t, 0.0, 1.0)
            t = np.where(seg_len2 > 0, t, 0.0)
            proj = a + t[:, None] * v  # (m, 2)
            diff = pts[i] - proj
            d2 = (diff * diff).sum(axis=1)
            j = int(np.argmin(d2))
            seg_idx[i] = j
            frac[i] = t[j]
            # Interpolate distance-along using the segment's distance-length
            # (which == GTFS shape_dist delta when GTFS dist was provided).
            dist_along[i] = self._cum_to_seg_start[j] + t[j] * seg_dist_len[j]
            perp[i] = math.sqrt(float(d2[j]))

        on_route = perp <= self.max_perp_m
        return MatchResult(
            segment_idx=seg_idx,
            frac=frac,
            dist_along_m=dist_along,
            perp_dist_m=perp,
            on_route=on_route,
        )
