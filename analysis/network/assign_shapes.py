"""Geometric trip→shape assignment.

The archive's ``trip_id`` (BusTime tatripid) does not join GTFS ``trips.txt``,
so each trip is assigned to a GTFS shape by matching its pings against every
candidate shape of its route and scoring:

    score = frac_on_route × frac_monotone

Wrong-direction shapes fail monotonicity (distance-along-shape runs backwards);
off-street shapes fail the on-route fraction. Among near-tied candidates,
prefer the shortest shape that contains the observed distance range — so a
short-turn trip lands on the short-turn variant, not the full-length shape
whose tail it never traversed.

Pure logic: matchers are injected, no I/O. Unit-tested on synthetic shapes.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

MIN_ON_ROUTE_PINGS = 10
MIN_SCORE = 0.75
LOW_CONFIDENCE_SCORE = 0.85  # below this, flag traversals as low-confidence
MONOTONE_MIN_MOVE_M = 30.0
TIE_MARGIN = 0.02

# Candidate-grid preselection (step A): shapes whose coarse tally score is
# within this margin of the coarse best get escalated to exact scoring.
# Generous on purpose — the grid must never decide a close call itself.
GRID_ESCALATE_MARGIN = 0.15
GRID_CELL_M = 250.0
GRID_PAD_M = 60.0  # ≥ snap max_perp so cell presence ≈ "could be on-route"


@dataclass(frozen=True)
class Assignment:
    shape_id: str
    score: float
    frac_on: float
    frac_monotone: float
    match: object  # MatchResult for the winning shape


def monotone_frac(d_on: np.ndarray, min_move_m: float = MONOTONE_MIN_MOVE_M) -> float:
    """Fraction of significant consecutive moves that go forward.

    Moves smaller than ``min_move_m`` are GPS jitter and ignored. With no
    significant moves at all the trip carries no direction information —
    return 0.0 so a stationary blob can't win a direction contest.
    """
    d_on = np.asarray(d_on, dtype=float)
    if len(d_on) < 2:
        return 0.0
    moves = np.diff(d_on)
    big = np.abs(moves) >= min_move_m
    if not big.any():
        return 0.0
    return float((moves[big] > 0).mean())


def score_shape(match, shape_len_m: float) -> tuple[float, float, float]:
    """(score, frac_on, frac_monotone) for one candidate's MatchResult."""
    on = match.on_route
    n = len(on)
    if n == 0 or on.sum() < 2:
        return 0.0, 0.0, 0.0
    frac_on = float(on.mean())
    fm = monotone_frac(match.dist_along_m[on])
    return frac_on * fm, frac_on, fm


class CandidateGrid:
    """Coarse cell → candidate-shape presence index over one route's shapes.

    Selection (step A) without projections: bin every candidate shape's
    segments (bbox padded by GRID_PAD_M) into GRID_CELL_M cells in a shared
    equirectangular frame; per (cell, shape) keep the first (lowest)
    dist-along as a coarse position. A trip then tallies, per shape, the
    fraction of pings landing in that shape's cells and the coarse
    monotonicity of their dist-alongs — an O(n_pings) stand-in for
    ``score = frac_on_route × frac_monotone``. The tally only PRESELECTS:
    near-tied shapes are escalated to the exact scorer (see choose_shape).
    """

    def __init__(
        self,
        matchers: dict[str, object],
        cell_m: float = GRID_CELL_M,
        pad_m: float = GRID_PAD_M,
    ):
        self.cell_m = float(cell_m)
        self.shape_ids = sorted(matchers)
        ref = matchers[self.shape_ids[0]]
        self._lat0, self._lon0 = ref._lat0, ref._lon0
        self._mlat, self._mlon = ref._mlat, ref._mlon
        grid: dict[tuple[int, int], dict[int, float]] = {}
        for si, sid in enumerate(self.shape_ids):
            mt = matchers[sid]
            pl = mt.polyline
            x = (pl[:, 1] - self._lon0) * self._mlon
            y = (pl[:, 0] - self._lat0) * self._mlat
            ax, ay, bx, by = x[:-1], y[:-1], x[1:], y[1:]
            cum0 = mt._cum_at_vert[:-1]
            cx0 = np.floor((np.minimum(ax, bx) - pad_m) / cell_m).astype(np.int64)
            cx1 = np.floor((np.maximum(ax, bx) + pad_m) / cell_m).astype(np.int64)
            cy0 = np.floor((np.minimum(ay, by) - pad_m) / cell_m).astype(np.int64)
            cy1 = np.floor((np.maximum(ay, by) + pad_m) / cell_m).astype(np.int64)
            for i in range(len(ax)):
                for gx in range(cx0[i], cx1[i] + 1):
                    for gy in range(cy0[i], cy1[i] + 1):
                        cellmap = grid.setdefault((gx, gy), {})
                        if si not in cellmap:
                            cellmap[si] = float(cum0[i])
        self._grid = {
            k: (
                np.fromiter(v.keys(), np.int64, len(v)),
                np.fromiter(v.values(), float, len(v)),
            )
            for k, v in grid.items()
        }

    def approx_scores(
        self, lats: np.ndarray, lons: np.ndarray
    ) -> dict[str, tuple[float, int]]:
        """shape_id -> (coarse score, cell-hit count); absent = zero hits."""
        x = (np.asarray(lons, dtype=float) - self._lon0) * self._mlon
        y = (np.asarray(lats, dtype=float) - self._lat0) * self._mlat
        cx = np.floor(x / self.cell_m).astype(np.int64)
        cy = np.floor(y / self.cell_m).astype(np.int64)
        n = len(x)
        dists: list[list[float]] = [[] for _ in self.shape_ids]
        for i in range(n):
            e = self._grid.get((int(cx[i]), int(cy[i])))
            if e is None:
                continue
            sids, dd = e
            for k in range(len(sids)):
                dists[sids[k]].append(dd[k])
        # Coarse monotonicity: dist-alongs are cell-quantized, so ignore
        # moves below ~a cell (same-cell repeats contribute nothing).
        min_move = max(self.cell_m * 0.9, MONOTONE_MIN_MOVE_M)
        out: dict[str, tuple[float, int]] = {}
        for si, lst in enumerate(dists):
            if not lst:
                continue
            frac_hit = len(lst) / n
            mono = monotone_frac(np.asarray(lst), min_move_m=min_move)
            out[self.shape_ids[si]] = (frac_hit * mono, len(lst))
        return out


_GRID_CACHE: dict[tuple[str, ...], CandidateGrid] = {}


def _grid_for(matchers: dict[str, object]) -> CandidateGrid:
    key = tuple(sorted(matchers))
    g = _GRID_CACHE.get(key)
    if g is None:
        g = _GRID_CACHE[key] = CandidateGrid(matchers)
    return g


def choose_shape(
    lats: np.ndarray,
    lons: np.ndarray,
    matchers: dict[str, object],  # shape_id -> SnapToShapeMatcher
    shape_len_m: dict[str, float],
    *,
    min_score: float = MIN_SCORE,
    min_on_route: int = MIN_ON_ROUTE_PINGS,
    tie_margin: float = TIE_MARGIN,
    use_grid: bool = True,
) -> Assignment | str:
    """Best shape for a trip, or a reject-reason string.

    Reject reasons: ``"no_candidates"``, ``"few_on_route"``, ``"low_score"``.

    With ``use_grid`` (default), a CandidateGrid tally preselects the
    contenders and only those are exact-scored. The grid never decides
    outcomes: near-ties (GRID_ESCALATE_MARGIN) are escalated together, and
    any contender-level reject re-runs the full exact scan so accept/reject
    boundaries and reject reasons match the brute path.
    """
    if not matchers:
        return "no_candidates"
    if use_grid and len(matchers) > 2:
        grid = _grid_for(matchers)
        approx = grid.approx_scores(lats, lons)
        if approx:
            a_best = max(v[0] for v in approx.values())
            contenders = {
                sid: matchers[sid]
                for sid, (sc, _) in approx.items()
                if sc >= a_best - GRID_ESCALATE_MARGIN
            }
            got = _choose_shape_exact(
                lats, lons, contenders, shape_len_m,
                min_score=min_score, min_on_route=min_on_route,
                tie_margin=tie_margin,
            )
            if not isinstance(got, str):
                return got
            # fall through: contender-level reject → decide via full scan
    return _choose_shape_exact(
        lats, lons, matchers, shape_len_m,
        min_score=min_score, min_on_route=min_on_route, tie_margin=tie_margin,
    )


def _choose_shape_exact(
    lats: np.ndarray,
    lons: np.ndarray,
    matchers: dict[str, object],
    shape_len_m: dict[str, float],
    *,
    min_score: float = MIN_SCORE,
    min_on_route: int = MIN_ON_ROUTE_PINGS,
    tie_margin: float = TIE_MARGIN,
) -> Assignment | str:
    """Exact scorer: snap against every given candidate (the pre-grid path).

    exact_far=False: scoring and every downstream consumer (run_reconstruct,
    delay_events) read only on-route rows, which stay bitwise-exact.
    """
    scored: list[tuple[str, float, float, float, object]] = []
    for shape_id, matcher in matchers.items():
        match = matcher.match(lats, lons, exact_far=False)
        score, frac_on, fm = score_shape(match, shape_len_m[shape_id])
        scored.append((shape_id, score, frac_on, fm, match))

    best_score = max(s[1] for s in scored)
    if best_score < min_score:
        # Distinguish "never on any shape" from "on-route but incoherent".
        best_on = max(int(s[4].on_route.sum()) for s in scored)
        return "few_on_route" if best_on < min_on_route else "low_score"

    # Near-ties: shortest shape containing the observed on-route span wins
    # (short-turn trips must not inherit the full-length shape's tail).
    contenders = [s for s in scored if s[1] >= best_score - tie_margin]
    def key(s):
        shape_id, _, _, _, match = s
        d_on = match.dist_along_m[match.on_route]
        d_max = float(d_on.max()) if len(d_on) else 0.0
        length = shape_len_m[shape_id]
        contains = length + 1.0 >= d_max  # always true by construction, kept for clarity
        return (not contains, length)

    shape_id, score, frac_on, fm, match = min(contenders, key=key)
    if int(match.on_route.sum()) < min_on_route:
        return "few_on_route"
    return Assignment(shape_id, score, frac_on, fm, match)
