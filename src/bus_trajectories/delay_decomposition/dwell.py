"""Dwell attribution — does a slowdown event belong to a bus stop?

The proximity-based default uses a per-stop dwell zone of [x_stop - back_m,
x_stop + ahead_m], clipped at any intersection node that falls inside the
default span (so a stop 12 m downstream of a signal has its upstream zone
end at the signal, not at x_stop - 30).

The protocol is the swap site for an AVL-based attributor once door-open /
door-close timestamps become available.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .events import Event
from .segments import Segment, StopOnRoute


@dataclass(frozen=True)
class DwellMatch:
    stop: StopOnRoute
    dwell_s: float


@runtime_checkable
class DwellAttributor(Protocol):
    """Decide whether an event is dwell at a stop in the segment."""

    def attribute(self, event: Event, segment: Segment) -> DwellMatch | None: ...


def _clip_zone(
    x_stop: float, back_m: float, ahead_m: float, segment: Segment
) -> tuple[float, float]:
    """Compute the (zone_lo, zone_hi) for a stop, clipped at intersections
    within the default span on either side and at segment boundaries."""
    zone_lo = max(x_stop - back_m, segment.x_start_m)
    zone_hi = min(x_stop + ahead_m, segment.x_end_m)
    for x_int in segment.intersection_xs:
        # An intersection between (x_stop - back_m, x_stop) tightens the back.
        if x_stop - back_m <= x_int < x_stop and x_int > zone_lo:
            zone_lo = x_int
        # An intersection between (x_stop, x_stop + ahead_m) tightens the front.
        if x_stop < x_int <= x_stop + ahead_m and x_int < zone_hi:
            zone_hi = x_int
    return zone_lo, zone_hi


@dataclass
class ProximityDwellAttributor:
    """Attribute an event to a stop if its distance span overlaps the stop's
    clipped dwell zone. The first overlapping stop (in route order) wins.
    """

    back_m: float = 30.0
    ahead_m: float = 10.0

    def attribute(self, event: Event, segment: Segment) -> DwellMatch | None:
        for stop in segment.stops:
            zone_lo, zone_hi = _clip_zone(
                stop.dist_along_m, self.back_m, self.ahead_m, segment
            )
            if event.x_start <= zone_hi and event.x_end >= zone_lo:
                return DwellMatch(stop=stop, dwell_s=event.duration_s)
        return None


@dataclass
class AVLDwellAttributor:
    """Future swap-in: read door-open / door-close timestamps from an AVL
    feed and attribute dwell by exact temporal overlap with each event.

    Intentionally not implemented — kept here so the swap point is obvious.
    Construct one of these and pass it to ``decompose_trip`` when AVL door
    data lands; the rest of the framework requires no changes.
    """

    avl_records: object = None  # placeholder — would carry pre-parsed AVL door events

    def attribute(self, event: Event, segment: Segment) -> DwellMatch | None:
        raise NotImplementedError(
            "AVLDwellAttributor is the integration point for AVL door-open/close "
            "data. Implement once the AVL feed is plumbed in; until then, use "
            "ProximityDwellAttributor()."
        )
