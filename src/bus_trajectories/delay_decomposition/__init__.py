"""Chapter-3-style delay decomposition for reconstructed bus trajectories.

Follows the methodology in *Chapter 3: Transit Delay Analysis* (Huang 2023
thesis) — signal-to-signal segmentation, per-segment travel time decomposed
into ``T_ff + T_dwell + D_signal + D_crossing + Loss + D_congestion``.

Two adjustments from the paper:
- Dwell time is attributed by proximity (no AVL door data available) via a
  pluggable :class:`DwellAttributor` interface; a future
  :class:`AVLDwellAttributor` swap is the obvious extension point.
- Mid-block pedestrian signals count as signalized intersections for
  segmentation purposes.
"""

from .segments import (
    NEAR_SIDE_THRESHOLD_M,
    Segment,
    StopOnRoute,
    build_segments,
    build_segments_for_pattern,
    build_segments_from_records,
)
from .events import (
    AbsoluteSpeedThreshold,
    Event,
    EventThreshold,
    detect_events,
)
from .dwell import (
    AVLDwellAttributor,
    DwellAttributor,
    ProximityDwellAttributor,
)
from .attribution import EventAttribution, attribute_event
from .loss import loss_time_for_event
from .travel_time import segment_freeflow_table, segment_observed_time
from .decompose import (
    SegmentDecomp,
    TripDecomp,
    aggregate_trips,
    decompose_trip,
)
from .feature_attribution import (
    FacilityMeta,
    build_facility_index,
    per_facility_seconds,
)

__all__ = [
    "NEAR_SIDE_THRESHOLD_M",
    "Segment",
    "StopOnRoute",
    "build_segments",
    "build_segments_for_pattern",
    "build_segments_from_records",
    "AbsoluteSpeedThreshold",
    "Event",
    "EventThreshold",
    "detect_events",
    "AVLDwellAttributor",
    "DwellAttributor",
    "ProximityDwellAttributor",
    "EventAttribution",
    "attribute_event",
    "loss_time_for_event",
    "segment_freeflow_table",
    "segment_observed_time",
    "SegmentDecomp",
    "TripDecomp",
    "aggregate_trips",
    "decompose_trip",
    "FacilityMeta",
    "build_facility_index",
    "per_facility_seconds",
]
