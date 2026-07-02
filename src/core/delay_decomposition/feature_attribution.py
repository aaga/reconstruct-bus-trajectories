"""Per-facility delay aggregation on top of the chapter-3 decomposition.

The chapter-3 decomposition tells us how much time each *segment* spent in
each category (dwell, signal_uniform, signal_overflow, crossing, slowdown).
For the G/H family of figures (per-feature tables, bar charts, route stem
plots, map bubbles) we instead want the answer keyed by *facility* — the
specific stop, signal, or crossing that absorbed the delay.

This module is a thin re-aggregation layer:

  - :func:`build_facility_index` walks the segment list and produces one
    :class:`FacilityMeta` per stop / signal / crossing along the route,
    keyed by the same ``facility_id`` strings that :mod:`attribution` writes
    into ``EventAttribution.facility_id``.

  - :func:`per_facility_seconds` consumes a :class:`TripDecomp` and returns
    per-facility totals + an "other" bucket holding the slowdown-event
    duration (the unattributed share of D_congestion).

Slowdown events have ``facility_id is None`` by design (their time stays in
D_congestion as a residual). They are summed into ``other_s`` here so the
G/H "OTHER" bucket matches what the naive scripts used to report.
"""

from __future__ import annotations

from dataclasses import dataclass

from dataio.intersections import ControlPoint
from .decompose import TripDecomp
from .segments import Segment


@dataclass(frozen=True)
class FacilityMeta:
    """Display metadata for one facility (stop / signal / crossing)."""

    facility_id: str
    label: str
    kind: str          # "stop", "stop_near_side", "signal", "crossing"
    dist_m: float
    lat: float | None
    lon: float | None


_SIGNAL_CTRL_LABEL = {
    "traffic_signals": "Traffic Signal",
    "ped_crossing_signal": "Ped Signal",
}
_CROSSING_CTRL_LABEL = {
    "ped_crossing_marked": "Crosswalk",
    "stop": "Stop Sign",
}


def _cross_str(cp: ControlPoint) -> str:
    return " / ".join(cp.cross_street_names) if cp.cross_street_names else "(unnamed)"


def build_facility_index(segments: list[Segment]) -> dict[str, FacilityMeta]:
    """Return one :class:`FacilityMeta` per stop / signal / crossing on the
    route, keyed by the same ``facility_id`` strings that
    :func:`attribution.attribute_event` writes into ``EventAttribution``.

    Conventions:
      * stops      -> ``stop_id`` verbatim
      * signals    -> ``SIG_{intersection_node_id}``
      * crossings  -> ``CX_{intersection_node_id}``
    """
    out: dict[str, FacilityMeta] = {}
    for s in segments:
        for stop in s.stops:
            kind = "stop_near_side" if stop.is_near_side else "stop"
            label = f"{stop.name} ({stop.stop_id})"
            out[stop.stop_id] = FacilityMeta(
                facility_id=stop.stop_id,
                label=label,
                kind=kind,
                dist_m=stop.dist_along_m,
                lat=None,
                lon=None,
            )
        # Downstream signal — the only signal that ever accumulates uniform
        # or overflow delay in this segment. (Upstream is the previous seg's
        # downstream — we will hit it when iterating the previous segment.)
        sig = s.downstream_signal
        sig_id = f"SIG_{sig.intersection_node_id}"
        if sig_id not in out:
            ctrl_label = _SIGNAL_CTRL_LABEL.get(sig.control_type, "Signal")
            out[sig_id] = FacilityMeta(
                facility_id=sig_id,
                label=f"{ctrl_label} @ {_cross_str(sig)}",
                kind="signal",
                dist_m=sig.dist_along_route_m,
                lat=sig.lat,
                lon=sig.lon,
            )
        # Non-signalized crossings strictly inside the segment.
        for cx in s.crossings:
            cx_id = f"CX_{cx.intersection_node_id}"
            if cx_id in out:
                continue
            ctrl_label = _CROSSING_CTRL_LABEL.get(cx.control_type, "Crossing")
            out[cx_id] = FacilityMeta(
                facility_id=cx_id,
                label=f"{ctrl_label} @ {_cross_str(cx)}",
                kind="crossing",
                dist_m=cx.dist_along_route_m,
                lat=cx.lat,
                lon=cx.lon,
            )
    return out


def per_facility_seconds(
    decomp: TripDecomp,
) -> tuple[dict[str, float], dict[tuple[str, str], float], float]:
    """Aggregate event durations across all segments of a single trip.

    Returns:
      * ``sec_per_facility``: facility_id -> total event-duration seconds.
        Signal totals fold ``signal_uniform`` + ``signal_overflow`` together
        since both attribute to the same signal facility.
      * ``sec_per_facility_category``: (facility_id, category) -> seconds.
        Lets callers split signal_uniform vs signal_overflow if they want.
      * ``other_s``: total slowdown-event duration (fallback bucket).
    """
    sec_per_facility: dict[str, float] = {}
    sec_per_facility_category: dict[tuple[str, str], float] = {}
    other_s = 0.0
    for seg in decomp.segments:
        for attr in seg.attributions:
            if attr.facility_id is None or attr.category == "slowdown":
                other_s += attr.core_s
                continue
            sec_per_facility[attr.facility_id] = (
                sec_per_facility.get(attr.facility_id, 0.0) + attr.core_s
            )
            cat_key = (attr.facility_id, attr.category)
            sec_per_facility_category[cat_key] = (
                sec_per_facility_category.get(cat_key, 0.0) + attr.core_s
            )
    return sec_per_facility, sec_per_facility_category, other_s
