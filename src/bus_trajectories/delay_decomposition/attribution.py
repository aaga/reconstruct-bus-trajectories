"""Route each detected event to a delay category.

Categories:
  - **dwell** — event overlaps a stop's clipped dwell zone in the primary
    segment. Near-side stops are flagged ``dwell_near_signal=True``.
  - **crossing** — event overlaps a non-signalized crossing's approach
    zone in the primary segment.
  - **signal_uniform** — event overlaps the downstream signal's approach
    zone (default 300 ft = 91.44 m upstream of the signal).
  - **signal_overflow** — assigned by a second pass in ``decompose_trip``:
    an unattributed (slowdown) event temporally preceding a
    ``signal_uniform`` event in the same primary segment with no
    intervening dwell/crossing is converted to overflow.
  - **slowdown** — fallback for events that don't fit any of the above.
    Slowdown time is *not* added to any facility bucket; it falls into
    the residual ``D_congestion`` term, so "slowdown" represents the
    portion of D_congestion that this code could resolve to discrete
    events without categorizing them.
"""

from __future__ import annotations

from dataclasses import dataclass

from .dwell import DwellAttributor
from .events import Event
from .segments import Segment

# 300 ft, the new uniform-signal approach threshold.
SIGNAL_UNIFORM_APPROACH_M = 91.44
CROSSING_APPROACH_M = 50.0


@dataclass(frozen=True)
class EventAttribution:
    event: Event
    category: str  # one of: dwell, crossing, signal_uniform, signal_overflow, slowdown
    facility_id: str | None
    core_s: float
    loss_s: float
    dwell_near_signal: bool = False


def attribute_event(
    event: Event,
    segment: Segment,
    dwell_attributor: DwellAttributor,
    loss_s: float,
    *,
    signal_approach_m: float = SIGNAL_UNIFORM_APPROACH_M,
    crossing_approach_m: float = CROSSING_APPROACH_M,
) -> EventAttribution:
    """Initial-pass categorization. Returns dwell, crossing, signal_uniform,
    or slowdown. The ``signal_overflow`` category is applied later by
    :func:`decompose.decompose_trip` as a second pass over the events.
    """
    # 1. Dwell? (Primary segment only — by design.)
    dwell_match = dwell_attributor.attribute(event, segment)
    if dwell_match is not None:
        return EventAttribution(
            event=event,
            category="dwell",
            facility_id=dwell_match.stop.stop_id,
            core_s=event.duration_s,
            loss_s=loss_s,
            dwell_near_signal=dwell_match.stop.is_near_side,
        )

    # 2. Crossing? (Non-signalized crossings inside the primary segment.)
    for cx in segment.crossings:
        x_cx = cx.dist_along_route_m
        if event.x_start <= x_cx + 10 and event.x_end >= x_cx - crossing_approach_m:
            return EventAttribution(
                event=event,
                category="crossing",
                facility_id=f"CX_{cx.intersection_node_id}",
                core_s=event.duration_s,
                loss_s=loss_s,
            )

    # 3. Signal uniform? (Within signal_approach_m of segment's downstream signal.)
    x_sig = segment.downstream_signal.dist_along_route_m
    if event.x_start <= x_sig + 10 and event.x_end >= x_sig - signal_approach_m:
        return EventAttribution(
            event=event,
            category="signal_uniform",
            facility_id=f"SIG_{segment.downstream_signal.intersection_node_id}",
            core_s=event.duration_s,
            loss_s=loss_s,
        )

    # 4. Fallback: slowdown. Time is absorbed into D_congestion as residual.
    return EventAttribution(
        event=event,
        category="slowdown",
        facility_id=None,
        core_s=event.duration_s,
        loss_s=loss_s,
    )
