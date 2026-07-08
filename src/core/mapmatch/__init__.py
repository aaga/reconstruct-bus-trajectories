"""Map-matching backends.

A backend implements the :class:`MapMatcher` protocol and returns a
:class:`MatchResult` of parallel arrays describing each ping's projection onto
a known route geometry.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np


@dataclass(frozen=True)
class MatchResult:
    """Per-ping projection onto a route geometry. All arrays have shape (n,)."""

    segment_idx: np.ndarray  # int — index of the polyline segment the ping snapped to
    frac: np.ndarray  # float in [0, 1] — fractional position along that segment
    dist_along_m: np.ndarray  # float — cumulative meters from polyline start
    perp_dist_m: np.ndarray  # float — perpendicular distance from ping to projection
    on_route: np.ndarray  # bool — perp_dist_m <= max_perp_m

    def __len__(self) -> int:
        return int(self.dist_along_m.shape[0])


class MapMatcher(Protocol):
    def match(self, lats: np.ndarray, lons: np.ndarray) -> MatchResult: ...


def get_matcher(name: str, **kwargs) -> MapMatcher:
    """Factory: ``shape_snap`` (default) or ``valhalla`` (stub)."""
    if name == "shape_snap":
        from .shape_snap import SnapToShapeMatcher

        return SnapToShapeMatcher(**kwargs)
    if name == "valhalla":
        from .valhalla import ValhallaMatcher

        return ValhallaMatcher(**kwargs)
    raise ValueError(f"unknown matcher {name!r}; choose 'shape_snap' or 'valhalla'")
