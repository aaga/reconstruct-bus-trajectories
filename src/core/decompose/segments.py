"""Signal-to-signal route segmentation.

A *segment* spans from just downstream of one signalized ControlPoint to just
downstream of the next. Signalized for our purposes = ``traffic_signals`` OR
``ped_crossing_signal`` (so mid-block ped signals act as segment boundaries
per chapter §3.2.3 and the user's specification).
"""

from __future__ import annotations

from dataclasses import dataclass

from core.control_points import (
    SIGNALIZED_CONTROL_TYPES,
    ControlPoint,
    classify_near_side_stops,
)

NEAR_SIDE_THRESHOLD_M = 90 / 3.28084  # 90 ft


@dataclass(frozen=True)
class StopOnRoute:
    stop_id: str
    name: str
    dist_along_m: float
    is_near_side: bool


@dataclass(frozen=True)
class Segment:
    seg_id: str
    x_start_m: float
    x_end_m: float
    upstream_signal: ControlPoint
    downstream_signal: ControlPoint
    stops: tuple[StopOnRoute, ...]
    crossings: tuple[ControlPoint, ...]
    intersection_xs: tuple[float, ...]  # all ControlPoint distances inside, sorted

    @property
    def length_m(self) -> float:
        return self.x_end_m - self.x_start_m


def _signalized(cp: ControlPoint) -> bool:
    return cp.control_type in SIGNALIZED_CONTROL_TYPES


def build_segments_from_records(
    control_points: list[ControlPoint],
    stops: list[dict],
    *,
    near_side_threshold_m: float = NEAR_SIDE_THRESHOLD_M,
) -> list[Segment]:
    """Construct segments given already-loaded ControlPoints + stops."""
    cps_sorted = sorted(control_points, key=lambda c: c.dist_along_route_m)
    signals = [c for c in cps_sorted if _signalized(c)]
    if len(signals) < 2:
        return []

    near_side_ids = classify_near_side_stops(
        stops, cps_sorted, threshold_m=near_side_threshold_m
    )

    segments: list[Segment] = []
    for upstream, downstream in zip(signals, signals[1:]):
        x_lo, x_hi = upstream.dist_along_route_m, downstream.dist_along_route_m
        # Stops fully inside (x_lo, x_hi]; exclude exact upstream signal location,
        # include x at downstream signal (stops at x_hi are "at" the downstream
        # signal — rare, but include for completeness).
        seg_stops = tuple(
            StopOnRoute(
                stop_id=str(s["stop_id"]),
                name=str(s["name"]),
                dist_along_m=float(s["dist_along_m"]),
                is_near_side=str(s["stop_id"]) in near_side_ids,
            )
            for s in stops
            if x_lo < float(s["dist_along_m"]) <= x_hi
        )
        seg_crossings = tuple(
            c for c in cps_sorted
            if x_lo < c.dist_along_route_m < x_hi
            and not _signalized(c)
        )
        seg_int_xs = tuple(
            c.dist_along_route_m for c in cps_sorted
            if x_lo < c.dist_along_route_m < x_hi
        )
        seg_id = f"SIG_{upstream.intersection_node_id}__SIG_{downstream.intersection_node_id}"
        segments.append(
            Segment(
                seg_id=seg_id,
                x_start_m=x_lo,
                x_end_m=x_hi,
                upstream_signal=upstream,
                downstream_signal=downstream,
                stops=seg_stops,
                crossings=seg_crossings,
                intersection_xs=seg_int_xs,
            )
        )
    return segments
