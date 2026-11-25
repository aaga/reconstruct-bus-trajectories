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

# Sequential (loop-aware) matching, 2026-09-30. Where a shape revisits a
# street (loops, out-and-back legs), the nearest projection can land on the
# wrong pass — a km-scale "backward" jump that fails the monotonicity gate
# (NYC Q70+, BX18A/B, Q102). With ``sequential=True`` each on-route ping
# keeps its best projection on EVERY pass within max_perp, and a Viterbi
# pass picks the sequence that is closest to the pings while not moving
# backward along the shape (DTW-style alignment at full resolution).
# Pings with a single pass keep exactly the nearest projection, and shapes
# that never come back within 2*max_perp of themselves skip the step, so
# results there are bitwise-identical to the plain matcher.
PASS_SPLIT_M = 100.0      # candidates further apart than this along the shape = different passes
SEQ_SIGMA_M = 15.0        # emission scale: cost (perp / sigma)^2
SEQ_BACK_FLOOR_M = 50.0   # backward moves up to this are GPS jitter (free)
SEQ_BACK_COST = 20.0      # flat cost of a backward move beyond the floor ...
SEQ_BACK_PER_M = 0.1      # ... plus this per metre
SEQ_FWD_PER_M = 1e-3      # tiny forward cost: ties prefer the earlier pass


def equirect_cumulative_m(polyline_latlon: np.ndarray) -> np.ndarray:
    """Cumulative along-polyline meters, equirectangular about the centroid.

    THE canonical route ruler for feeds without ``shape_dist_traveled``
    (MBTA omits it): identical to SnapToShapeMatcher's internal fallback,
    so distances computed anywhere in the pipeline line up exactly.
    """
    lat0 = float(polyline_latlon[:, 0].mean())
    mlat = 111320.0
    mlon = 111320.0 * math.cos(math.radians(lat0))
    dx = np.diff(polyline_latlon[:, 1]) * mlon
    dy = np.diff(polyline_latlon[:, 0]) * mlat
    cum = np.zeros(polyline_latlon.shape[0])
    cum[1:] = np.cumsum(np.hypot(dx, dy))
    return cum


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
        sequential: bool = False,
    ):
        if polyline_latlon.ndim != 2 or polyline_latlon.shape[1] != 2:
            raise ValueError("polyline_latlon must have shape (N, 2) of (lat, lon)")
        if polyline_latlon.shape[0] < 2:
            raise ValueError("polyline must have at least 2 vertices")
        self.polyline = polyline_latlon
        self.max_perp_m = float(max_perp_m)
        self.sequential = bool(sequential)

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
        self.self_overlaps = self._detect_self_overlap() if self.sequential else False

    def _detect_self_overlap(self) -> bool:
        """True if the shape comes back within 2*max_perp of itself.

        A conservative fast skip for sequential matching (sharp curves can
        trip it too; the per-ping ambiguity test decides what actually
        changes). Densified at 10 m; pairs closer than 2*max_perp in space
        but more than PASS_SPLIT_M + 2*max_perp apart along the shape.
        """
        from scipy.spatial import cKDTree

        pts, cum = [], []
        for i in range(len(self._seg_a)):
            L = float(self._proj_seg_len[i])
            k = max(1, int(L // 10.0))
            t = np.arange(k) / k
            pts.append(self._seg_a[i] + t[:, None] * self._seg_v[i])
            cum.append(self._cum_to_seg_start[i] + t * self._seg_dist_len[i])
        pts = np.concatenate(pts)
        cum = np.concatenate(cum)
        pairs = cKDTree(pts).query_pairs(2 * self.max_perp_m, output_type="ndarray")
        if len(pairs) == 0:
            return False
        gap = np.abs(cum[pairs[:, 0]] - cum[pairs[:, 1]])
        return bool((gap > PASS_SPLIT_M + 2 * self.max_perp_m).any())

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
        self, lats: np.ndarray, lons: np.ndarray, *, exact_far: bool = True,
        sequential: bool | None = None,
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
            seq = self.sequential if sequential is None else sequential
            if seq and self.self_overlaps:
                self._sequential_fix(covered, C, t, d2, seg_idx, frac,
                                     dist_along, perp)
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

    def _sequential_fix(self, covered, C, t, d2, seg_idx, frac, dist_along,
                        perp) -> None:
        """Re-pick projections for ambiguous pings by a monotone Viterbi.

        Rows of C/t/d2 correspond to ``covered`` (time order preserved).
        Only on-route rows take part; a ping is ambiguous when its within-
        max_perp candidates span more than PASS_SPLIT_M along the shape.
        A single-candidate ping pins the path, so the global optimum splits
        into independent runs of consecutive ambiguous pings, each anchored
        by the pinned pings around it — solved run by run, which is exactly
        equivalent and touches only the ambiguous rows.
        """
        lim = self.max_perp_m ** 2
        ok = d2 <= lim                                       # (nc, g)
        on = ok.any(axis=1)
        da = self._cum_to_seg_start[C] + t * self._seg_dist_len[C]
        big = np.where(ok, da, -np.inf).max(axis=1)
        small = np.where(ok, da, np.inf).min(axis=1)
        amb = on & ((big - small) > PASS_SPLIT_M)
        if not amb.any():
            return
        rows = np.flatnonzero(on)                            # on-route, time order
        amb_on = amb[rows]
        near_da = dist_along[covered[rows]]                  # nearest projection
        # Candidate passes for ALL ambiguous rows at once (per-row numpy calls
        # dominated the cost): sort each row's in-radius candidates by
        # distance-along, cut where consecutive ones are > PASS_SPLIT_M apart,
        # keep the nearest projection of each pass.
        ar = np.flatnonzero(amb)
        daA = np.where(ok[ar], da[ar], np.inf)
        order = np.argsort(daA, axis=1)
        dsort = np.take_along_axis(daA, order, axis=1)
        valid = np.isfinite(dsort)
        newpass = np.zeros_like(valid)
        with np.errstate(invalid="ignore"):              # inf - inf in padding
            newpass[:, 1:] = (np.diff(dsort, axis=1) > PASS_SPLIT_M) & valid[:, 1:]
        pid = np.cumsum(newpass, axis=1)
        rr_, cc_ = np.nonzero(valid)
        colj = order[rr_, cc_]
        grp = rr_ * (daA.shape[1] + 1) + pid[rr_, cc_]
        key_d2 = d2[ar[rr_], colj]
        o = np.lexsort((key_d2, grp))
        first = np.r_[True, grp[o][1:] != grp[o][:-1]]
        sel = o[first]                                      # nearest of each (row, pass)
        cand_row = ar[rr_[sel]]
        cand_col = colj[sel]
        cand = {}
        for r, c in zip(cand_row.tolist(), cand_col.tolist()):
            cand.setdefault(r, []).append(
                (float(da[r, c]), float(d2[r, c]), int(C[r, c]), float(t[r, c])))
        # runs of consecutive ambiguous on-route rows
        edges = np.flatnonzero(np.diff(np.r_[0, amb_on.astype(np.int8), 0]))
        s2 = SEQ_SIGMA_M ** 2

        def trans(d_a, d_b):
            mv = d_b - d_a
            if mv >= -SEQ_BACK_FLOOR_M:
                return SEQ_FWD_PER_M * max(mv, 0.0)
            return SEQ_BACK_COST + SEQ_BACK_PER_M * (-mv - SEQ_BACK_FLOOR_M)

        for lo, hi in zip(edges[::2], edges[1::2]):          # rows[lo:hi] ambiguous
            prev_d = float(near_da[lo - 1]) if lo > 0 else None
            next_d = float(near_da[hi]) if hi < len(rows) else None
            states = [cand[r] for r in rows[lo:hi].tolist()]
            cost = [(trans(prev_d, dv) if prev_d is not None else 0.0) + dd / s2
                    for dv, dd, _, _ in states[0]]
            back = []
            for k in range(1, len(states)):
                prev, nc, bp = states[k - 1], [], []
                for (d_b, dd_b, _, _) in states[k]:
                    best, arg = math.inf, 0
                    for ia, (d_a, _, _, _) in enumerate(prev):
                        c = cost[ia] + trans(d_a, d_b)
                        if c < best:
                            best, arg = c, ia
                    nc.append(best + dd_b / s2)
                    bp.append(arg)
                cost = nc
                back.append(bp)
            if next_d is not None:
                cost = [c + trans(dv, next_d) for c, (dv, _, _, _) in zip(cost, states[-1])]
            pick = [0] * len(states)
            pick[-1] = int(np.argmin(cost))
            for k in range(len(states) - 1, 0, -1):
                pick[k - 1] = back[k - 1][pick[k]]
            for k, r in enumerate(rows[lo:hi]):
                d_v, dd_v, sg, fr = states[k][pick[k]]
                i = covered[r]
                seg_idx[i] = sg
                frac[i] = fr
                dist_along[i] = d_v
                perp[i] = math.sqrt(dd_v)

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
