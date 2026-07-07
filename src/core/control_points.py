"""The ControlPoint data model + pure helpers over it.

This is the *shape* of a controlled feature along a route (signals, stop/give-way
signs, pedestrian crossings) plus the pure geometry that classifies it — no I/O.
The Overpass enrichment that *produces* ControlPoints and the on-disk
(de)serialization live in ``dataio.intersections`` (which re-exports these names
for backwards compatibility). Keeping the model here lets ``core`` reason about
segments/attribution without importing ``dataio``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ControlPoint:
    """One controlled feature along a route.

    For traffic_signals / stop / give_way, the ``intersection_node_id`` is
    the topological intersection vertex on the bus's way (the node OSM
    shares between the bus's road and a cross-street).

    For ped_crossing_signal / ped_crossing_marked the ``intersection_node_id``
    is the crossing's own OSM node id; the *intersection* it belongs to
    (if any) is recorded separately in ``anchor_intersection_node_id``.
    """

    intersection_node_id: int
    lat: float
    lon: float
    dist_along_route_m: float
    on_way_id: int
    control_type: str  # "traffic_signals" | "stop" | "give_way" | "ped_crossing_signal" | "ped_crossing_marked"
    cross_street_names: tuple[str, ...]
    # When ControlPoints get merged (e.g., two signal nodes that are
    # really one physical intersection split across two OSM ways), this
    # tracks the OSM node_ids that were folded into this representative.
    merged_node_ids: tuple[int, ...] = ()

    # Pedestrian-crossing metadata. Populated only for ped_crossing_*
    # ControlPoints; default-empty for everything else.
    #
    # ``anchor_intersection_node_id`` — the intersection vertex on the bus's
    # way that this crossing belongs to, or ``None`` for a true mid-block
    # crossing with no intersection vertex nearby. Multiple crossings at
    # the same physical intersection share the same anchor id.
    #
    # ``signalized`` — derived from ``control_type`` for ped_crossing_*
    # but stored explicitly so downstream code can filter on a single
    # boolean without parsing the type string.
    #
    # ``markings`` — the value of OSM ``crossing:markings`` (e.g. "zebra",
    # "lines", "dashes", "ladder", "yes"). Empty string if absent.
    #
    # ``has_island`` — true iff OSM ``crossing:island=yes`` (a pedestrian
    # refuge in the middle of the crossing).
    anchor_intersection_node_id: int | None = None
    signalized: bool = False
    markings: str = ""
    has_island: bool = False


# Control types that act as signal-to-signal segment boundaries.
SIGNALIZED_CONTROL_TYPES = frozenset({"traffic_signals", "ped_crossing_signal"})


def classify_near_side_stops(
    stops: list[dict],
    control_points: list[ControlPoint],
    threshold_m: float = 90 / 3.28084,  # 90 ft
) -> set[str]:
    """Return the set of ``stop_id`` values that are "near-side" — i.e. have any
    signalized ControlPoint (``traffic_signals`` or ``ped_crossing_signal``)
    within ``threshold_m`` *downstream* (larger ``dist_along_route_m``).

    For near-side stops, the dwell vs. signal portion of any stopping activity
    is ambiguous from GPS alone; downstream consumers should flag attributions
    to these stops as uncertain.
    """
    signal_xs = sorted(
        cp.dist_along_route_m for cp in control_points
        if cp.control_type in SIGNALIZED_CONTROL_TYPES
    )
    flagged: set[str] = set()
    for stop in stops:
        x_stop = stop["dist_along_m"]
        for x_sig in signal_xs:
            if x_sig < x_stop:
                continue
            if x_sig - x_stop > threshold_m:
                break  # sorted; no further signals can be within threshold
            flagged.add(str(stop["stop_id"]))
            break
    return flagged
