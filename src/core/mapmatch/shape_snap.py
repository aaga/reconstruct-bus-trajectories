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

# Spatial-index cell size for the per-shape segment grid. Segments register
# into every cell their bbox (padded by max_perp_m) touches, so a ping only
# ever needs its OWN cell's candidate list — any segment within max_perp of
# the ping is guaranteed to be registered there.
DEFAULT_GRID_CELL_M = 200.0


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

        # Segment grid: cell -> ascending array of segment indices whose
        # padded bbox touches the cell. Registration padding = max_perp_m,
        # so a single-cell lookup is exact for any possibly-on-route ping
        # (see match()). Built once per matcher; matchers are cached
        # per-shape by the batch drivers.
        cell = self._grid_cell_m = DEFAULT_GRID_CELL_M
        pad = self.max_perp_m
        lo = np.minimum(a, b) - pad
        hi = np.maximum(a, b) + pad
        cx0 = np.floor(lo[:, 0] / cell).astype(np.int64)
        cx1 = np.floor(hi[:, 0] / cell).astype(np.int64)
        cy0 = np.floor(lo[:, 1] / cell).astype(np.int64)
        cy1 = np.floor(hi[:, 1] / cell).astype(np.int64)
        grid: dict[tuple[int, int], list[int]] = {}
        for i in range(len(a)):
            for gx in range(cx0[i], cx1[i] + 1):
                for gy in range(cy0[i], cy1[i] + 1):
                    grid.setdefault((gx, gy), []).append(i)
        self._grid = {k: np.asarray(v, dtype=np.int64) for k, v in grid.items()}

    # ------------------------------------------------------------------ utils
    def _project(self, lats: np.ndarray, lons: np.ndarray) -> np.ndarray:
        x = (np.asarray(lons, dtype=float) - self._lon0) * self._mlon
        y = (np.asarray(lats, dtype=float) - self._lat0) * self._mlat
        return np.column_stack([x, y])

    def _project_rows(
        self,
        pts: np.ndarray,
        rows: np.ndarray,
        cand: np.ndarray,
        seg_idx: np.ndarray,
        frac: np.ndarray,
        dist_along: np.ndarray,
        perp: np.ndarray,
    ) -> None:
        """Nearest-of-``cand`` projection for ping rows; fills output arrays.

        Blocked over pings so the (k, len(cand), 2) broadcast stays cache
        friendly. ``cand`` must be ascending so argmin tie-breaking matches
        a full scan (profiling 2026-07: the original per-ping Python loop
        over ALL segments was 94% of the network batch's wall time).
        """
        a = self._seg_a[cand]
        v = self._seg_v[cand]
        seg_len2 = self._seg_len2[cand]
        seg_dist_len = self._seg_dist_len[cand]
        cum0 = self._cum_to_seg_start[cand]
        # Avoid division by zero on degenerate (zero-length) segments.
        safe_len2 = np.where(seg_len2 > 0, seg_len2, 1.0)
        degenerate = seg_len2 <= 0
        has_degenerate = bool(degenerate.any())

        BLOCK = 32
        for lo in range(0, len(rows), BLOCK):
            r = rows[lo : lo + BLOCK]
            blk = pts[r][:, None, :]               # (k, 1, 2)
            d = blk - a[None, :, :]                # (k, g, 2)
            t = (d * v).sum(axis=2) / safe_len2    # (k, g)
            np.clip(t, 0.0, 1.0, out=t)
            if has_degenerate:
                t[:, degenerate] = 0.0
            proj = a[None, :, :] + t[..., None] * v[None, :, :]
            diff = blk - proj
            d2 = (diff * diff).sum(axis=2)         # (k, g)
            j = np.argmin(d2, axis=1)              # (k,)
            rr = np.arange(len(r))
            tj = t[rr, j]
            seg_idx[r] = cand[j]
            frac[r] = tj
            # Interpolate distance-along using the segment's distance-length
            # (which == GTFS shape_dist delta when GTFS dist was provided).
            dist_along[r] = cum0[j] + tj * seg_dist_len[j]
            perp[r] = np.sqrt(d2[rr, j])

    # ------------------------------------------------------------------ match
    def match(
        self, lats: np.ndarray, lons: np.ndarray, *, exact_far: bool = True
    ) -> MatchResult:
        """Grid-accelerated snap.

        Each ping projects only onto the segments registered in its grid
        cell. Because segments register with max_perp padding, the cell's
        candidate list provably contains the GLOBAL nearest segment for any
        ping within max_perp of the shape — so the on_route mask and every
        on-route row are always bitwise-identical to ``match_brute``.

        ``exact_far=True`` (default) additionally reruns pings whose
        grid-local result exceeds max_perp against the full scan, making
        ALL outputs bitwise-identical — required by consumers like
        way_match that read dist_along for beyond-threshold points.
        Scoring/batch callers that only consume on-route rows should pass
        ``exact_far=False``: a wrong-direction candidate can put half a
        trip's pings off-route, and full-scanning those would erase the
        grid's win (off-route rows then hold the nearest IN-CELL segment,
        or perp=inf for empty cells).
        """
        pts = self._project(lats, lons)  # (n, 2)
        n = pts.shape[0]
        m = self._seg_a.shape[0]

        seg_idx = np.zeros(n, dtype=np.int64)
        frac = np.zeros(n, dtype=float)
        dist_along = np.zeros(n, dtype=float)
        perp = np.full(n, np.inf)

        cx = np.floor(pts[:, 0] / self._grid_cell_m).astype(np.int64)
        cy = np.floor(pts[:, 1] / self._grid_cell_m).astype(np.int64)
        get = self._grid.get
        cands = [get((int(cx[i]), int(cy[i]))) for i in range(n)]
        glens = np.fromiter(
            (0 if c is None else len(c) for c in cands), np.int64, n
        )
        covered = np.nonzero(glens > 0)[0]
        if len(covered):
            # One padded (n_covered, max_g) candidate matrix → a single
            # broadcast, not one numpy call per cell (call overhead was the
            # bottleneck). Padding entries are masked to +inf distance;
            # candidate lists are ascending so argmin ties break exactly
            # like a full scan.
            max_g = int(glens[covered].max())
            C = np.zeros((len(covered), max_g), dtype=np.int64)
            valid = np.zeros((len(covered), max_g), dtype=bool)
            for k, i in enumerate(covered):
                c = cands[i]
                C[k, : len(c)] = c
                valid[k, : len(c)] = True
            a = self._seg_a[C]                     # (nc, g, 2)
            v = self._seg_v[C]
            len2 = self._seg_len2[C]
            safe = np.where(len2 > 0, len2, 1.0)
            p = pts[covered][:, None, :]
            d = p - a
            t = (d * v).sum(axis=2) / safe
            np.clip(t, 0.0, 1.0, out=t)
            t[len2 <= 0] = 0.0
            proj = a + t[..., None] * v
            diff = p - proj
            d2 = (diff * diff).sum(axis=2)
            d2[~valid] = np.inf
            j = np.argmin(d2, axis=1)
            rows = np.arange(len(covered))
            tj = t[rows, j]
            jj = C[rows, j]
            seg_idx[covered] = jj
            frac[covered] = tj
            dist_along[covered] = (
                self._cum_to_seg_start[jj] + tj * self._seg_dist_len[jj]
            )
            perp[covered] = np.sqrt(d2[rows, j])
        if exact_far:
            # Beyond-threshold (or empty-cell) pings: the padding proof only
            # covers distances <= max_perp, so rerun those against all
            # segments for exact far-side values.
            redo = np.nonzero(perp > self.max_perp_m)[0]
            if len(redo):
                self._project_rows(
                    pts, redo, np.arange(m, dtype=np.int64),
                    seg_idx, frac, dist_along, perp,
                )

        on_route = perp <= self.max_perp_m
        return MatchResult(
            segment_idx=seg_idx,
            frac=frac,
            dist_along_m=dist_along,
            perp_dist_m=perp,
            on_route=on_route,
        )

    def match_brute(self, lats: np.ndarray, lons: np.ndarray) -> MatchResult:
        """Full-scan reference implementation (testing / verification)."""
        pts = self._project(lats, lons)
        n = pts.shape[0]
        m = self._seg_a.shape[0]
        seg_idx = np.zeros(n, dtype=np.int64)
        frac = np.zeros(n, dtype=float)
        dist_along = np.zeros(n, dtype=float)
        perp = np.full(n, np.inf)
        self._project_rows(
            pts, np.arange(n, dtype=np.int64), np.arange(m, dtype=np.int64),
            seg_idx, frac, dist_along, perp,
        )
        on_route = perp <= self.max_perp_m
        return MatchResult(
            segment_idx=seg_idx,
            frac=frac,
            dist_along_m=dist_along,
            perp_dist_m=perp,
            on_route=on_route,
        )
