"""Valhalla-backed map matcher (stub).

The original paper (Huang et al., ITSC 2023, §III-B) uses Valhalla's
``trace_attributes`` service to snap GPS pings to OSM road segments and then
verifies that the matched segment lies on a predefined route. To run this
backend you need a local Valhalla server with an OSM extract covering the
service area.

Sketch of the request payload (per the paper and Valhalla docs):

    POST {endpoint_url}/trace_attributes
    {
      "shape": [{"lat": ..., "lon": ..., "time": ...}, ...],
      "shape_match": "map_snap",
      "costing": "bus",
      "filters": {
        "attributes": ["edge.way_id", "edge.length", "matched.point",
                        "matched.distance_along_edge", "matched.distance_from_trace_point"]
      }
    }

To wire this up: parse ``edges`` + ``matched_points``, then for each
``matched_point`` translate ``edge_index`` + ``distance_along_edge`` into a
``MatchResult`` against the predefined route geometry.
"""

from __future__ import annotations

import numpy as np

from . import MatchResult


class ValhallaMatcher:
    def __init__(self, endpoint_url: str = "http://localhost:8002", **_: object):
        self.endpoint_url = endpoint_url

    def match(self, lats: np.ndarray, lons: np.ndarray) -> MatchResult:  # noqa: ARG002
        raise NotImplementedError(
            "Valhalla backend not yet wired up. Use matcher='shape_snap'. "
            "See module docstring for the request shape needed to implement this."
        )
