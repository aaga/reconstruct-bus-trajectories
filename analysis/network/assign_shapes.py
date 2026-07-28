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


def choose_shape(
    lats: np.ndarray,
    lons: np.ndarray,
    matchers: dict[str, object],  # shape_id -> SnapToShapeMatcher
    shape_len_m: dict[str, float],
    *,
    min_score: float = MIN_SCORE,
    min_on_route: int = MIN_ON_ROUTE_PINGS,
    tie_margin: float = TIE_MARGIN,
) -> Assignment | str:
    """Best shape for a trip, or a reject-reason string.

    Reject reasons: ``"no_candidates"``, ``"few_on_route"``, ``"low_score"``.
    """
    if not matchers:
        return "no_candidates"

    scored: list[tuple[str, float, float, float, object]] = []
    for shape_id, matcher in matchers.items():
        match = matcher.match(lats, lons)
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
