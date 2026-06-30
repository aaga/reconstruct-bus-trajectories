"""Per-trip and per-corridor delay decomposition orchestrator.

Implements the chapter §3.3 decomposition:

    T_obs = T_ff + T_dwell + D_signal + D_crossing + D_congestion

where ``D_signal = D_signal_uniform + D_signal_overflow``, per signal-to-
signal segment. The dwell attributor is pluggable so AVL-based attribution
can be swapped in once available.

The "slowdown" event category exists as a diagnostic label for events that
weren't attributed to any facility; their time is **not** added to any
bucket and falls into the residual ``D_congestion`` term.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np
import pandas as pd

from ..serialize import from_pchip_record, load_records
from .attribution import EventAttribution, attribute_event
from .dwell import DwellAttributor, ProximityDwellAttributor
from .events import AbsoluteSpeedThreshold, Event, EventThreshold, detect_events
from .loss import M_PER_S_TO_MPH, loss_shoulders_for_event
from .segments import Segment
from .travel_time import _last_t_at_x, segment_observed_time

DEFAULT_DENSE_DT_S = 2.0


@dataclass
class SegmentDecomp:
    """Per-segment travel-time decomposition.

    t_obs = t_ff + t_dwell + d_signal + d_crossing + d_congestion
    d_signal = d_signal_uniform + d_signal_overflow

    Each bucket = core + loss. "slowdown" events live in d_congestion as
    residual (no bucket claims them).
    """

    seg_id: str
    t_obs: float
    t_ff: float
    t_dwell: float
    d_signal_uniform: float
    d_signal_overflow: float
    d_signal: float           # = d_signal_uniform + d_signal_overflow
    d_crossing: float
    d_congestion: float       # residual, absorbs slowdown time
    t_dwell_core: float
    t_dwell_loss: float
    d_signal_uniform_core: float
    d_signal_uniform_loss: float
    d_signal_overflow_core: float
    d_signal_overflow_loss: float
    d_crossing_core: float
    d_crossing_loss: float
    t_dwell_near_signal: float
    attributions: list[EventAttribution] = field(default_factory=list)


@dataclass
class TripDecomp:
    trip_id: str
    segments: list[SegmentDecomp]

    @property
    def t_obs_total(self) -> float:
        return sum(s.t_obs for s in self.segments)


def _evaluate_dense(record: dict, dt_s: float):
    f = from_pchip_record(record)
    t0, t1 = float(f.x[0]), float(f.x[-1])
    t = np.arange(t0, t1 + dt_s, dt_s)
    t = np.clip(t, t0, t1)
    x = np.asarray(f(t), dtype=float)
    v_mps = np.asarray(f.derivative()(t), dtype=float)
    v_mph = v_mps * M_PER_S_TO_MPH
    return f, t, x, v_mph


@dataclass
class _Acc:
    attributions: list[EventAttribution] = field(default_factory=list)
    core_dwell: float = 0.0
    loss_dwell: float = 0.0
    core_signal_uniform: float = 0.0
    loss_signal_uniform: float = 0.0
    core_signal_overflow: float = 0.0
    loss_signal_overflow: float = 0.0
    core_crossing: float = 0.0
    loss_crossing: float = 0.0
    t_dwell_near_signal: float = 0.0


def _apply_overflow_pass(
    event_records: list[tuple[Event, EventAttribution, Segment]],
) -> list[tuple[Event, EventAttribution, Segment]]:
    """Convert slowdown events that temporally precede a signal_uniform
    event in the SAME primary segment (with no dwell/crossing in between)
    into signal_overflow events attributed to the same signal facility.

    Walk strictly backward through slowdowns: stop at any non-slowdown
    event (including dwell, crossing, signal_uniform, signal_overflow).
    """
    by_seg: dict[str, list[int]] = {}
    for idx, (_, _, primary) in enumerate(event_records):
        by_seg.setdefault(primary.seg_id, []).append(idx)

    out = list(event_records)
    for seg_id, idxs in by_seg.items():
        # Sort by event.t_start.
        idxs = sorted(idxs, key=lambda i: out[i][0].t_start)
        for pos, i in enumerate(idxs):
            _, attr, _ = out[i]
            if attr.category != "signal_uniform":
                continue
            signal_facility_id = attr.facility_id
            # Walk backward through slowdowns only.
            for j in range(pos - 1, -1, -1):
                k = idxs[j]
                prev_ev, prev_attr, prev_primary = out[k]
                if prev_attr.category != "slowdown":
                    break
                new_attr = replace(
                    prev_attr,
                    category="signal_overflow",
                    facility_id=signal_facility_id,
                )
                out[k] = (prev_ev, new_attr, prev_primary)
    return out


def decompose_trip(
    record: dict,
    segments: list[Segment],
    freeflow_t_obs: dict[str, float],
    *,
    threshold: EventThreshold | None = None,
    dwell_attributor: DwellAttributor | None = None,
    dense_dt_s: float = DEFAULT_DENSE_DT_S,
    cruise_threshold_mph: float = 12.0,
    include_loss: bool = False,
    min_duration_s: float = 15.0,
) -> TripDecomp:
    """Decompose one reconstructed trip into per-segment delay components.

    Parameters
    ----------
    include_loss
        When True, accel/decel shoulders are folded into the parent
        facility bucket. Off by default.
    min_duration_s
        Minimum slowdown duration for an event to be detected (passed to
        ``detect_events``). Default 15 s (chapter §3.3).
    """
    threshold = threshold or AbsoluteSpeedThreshold(5.0)
    dwell_attributor = dwell_attributor or ProximityDwellAttributor()

    f, t, x, v_mph = _evaluate_dense(record, dense_dt_s)
    all_events = detect_events(t, x, v_mph, threshold, min_duration_s=min_duration_s)

    # Pre-compute each segment's time bounds.
    seg_bounds: dict[str, tuple[float, float]] = {
        seg.seg_id: (
            _last_t_at_x(f, seg.x_start_m),
            _last_t_at_x(f, seg.x_end_m),
        )
        for seg in segments
    }

    def _primary_seg_by_time(ev: Event) -> Segment | None:
        """Primary = segment that holds the MAJORITY of the event's time."""
        best_seg = None
        best_dur = 0.0
        for s in segments:
            t_lo, t_hi = seg_bounds[s.seg_id]
            dur = max(0.0, min(t_hi, ev.t_end) - max(t_lo, ev.t_start))
            if dur > best_dur:
                best_dur = dur
                best_seg = s
        return best_seg

    # --- First pass: categorize each event in its primary segment context.
    event_records: list[tuple[Event, EventAttribution, Segment]] = []
    loss_intervals: dict[int, tuple[tuple[float, float], tuple[float, float]]] = {}
    for ev in all_events:
        primary = _primary_seg_by_time(ev)
        if primary is None:
            continue
        if include_loss:
            decel_int, accel_int = loss_shoulders_for_event(
                t, v_mph, ev, cruise_threshold_mph=cruise_threshold_mph
            )
            total_loss_s = (decel_int[1] - decel_int[0]) + (accel_int[1] - accel_int[0])
        else:
            decel_int = (ev.t_start, ev.t_start)
            accel_int = (ev.t_end, ev.t_end)
            total_loss_s = 0.0
        attr = attribute_event(ev, primary, dwell_attributor, total_loss_s)
        event_records.append((ev, attr, primary))
        loss_intervals[len(event_records) - 1] = (decel_int, accel_int)

    # --- Second pass: overflow conversion (slowdown -> signal_overflow).
    event_records = _apply_overflow_pass(event_records)

    # --- Third pass: accumulate (clipped) durations into segment buckets.
    acc: dict[str, _Acc] = {s.seg_id: _Acc() for s in segments}
    for idx, (ev, attr, primary) in enumerate(event_records):
        decel_int, accel_int = loss_intervals[idx]
        acc[primary.seg_id].attributions.append(attr)
        for s in segments:
            t_lo, t_hi = seg_bounds[s.seg_id]
            core_in_seg = max(0.0, min(t_hi, ev.t_end) - max(t_lo, ev.t_start))
            decel_in_seg = max(
                0.0, min(t_hi, decel_int[1]) - max(t_lo, decel_int[0])
            )
            accel_in_seg = max(
                0.0, min(t_hi, accel_int[1]) - max(t_lo, accel_int[0])
            )
            loss_in_seg = decel_in_seg + accel_in_seg
            if core_in_seg + loss_in_seg <= 0:
                continue
            a = acc[s.seg_id]
            if attr.category == "dwell":
                a.core_dwell += core_in_seg
                a.loss_dwell += loss_in_seg
                if attr.dwell_near_signal:
                    a.t_dwell_near_signal += core_in_seg + loss_in_seg
            elif attr.category == "signal_uniform":
                a.core_signal_uniform += core_in_seg
                a.loss_signal_uniform += loss_in_seg
            elif attr.category == "signal_overflow":
                a.core_signal_overflow += core_in_seg
                a.loss_signal_overflow += loss_in_seg
            elif attr.category == "crossing":
                a.core_crossing += core_in_seg
                a.loss_crossing += loss_in_seg
            # "slowdown" events: not added to any bucket → fall into D_congestion.

    # --- Build SegmentDecomp records.
    seg_decomps: list[SegmentDecomp] = []
    for seg in segments:
        a = acc[seg.seg_id]
        t_dwell = a.core_dwell + a.loss_dwell
        d_signal_uniform = a.core_signal_uniform + a.loss_signal_uniform
        d_signal_overflow = a.core_signal_overflow + a.loss_signal_overflow
        d_signal = d_signal_uniform + d_signal_overflow
        d_crossing = a.core_crossing + a.loss_crossing

        t_obs = segment_observed_time(f, seg)
        t_ff = freeflow_t_obs.get(seg.seg_id, float("nan"))
        if np.isnan(t_ff):
            d_congestion = float("nan")
        else:
            d_congestion = t_obs - t_ff - t_dwell - d_signal - d_crossing

        seg_decomps.append(
            SegmentDecomp(
                seg_id=seg.seg_id,
                t_obs=t_obs,
                t_ff=t_ff,
                t_dwell=t_dwell,
                d_signal_uniform=d_signal_uniform,
                d_signal_overflow=d_signal_overflow,
                d_signal=d_signal,
                d_crossing=d_crossing,
                d_congestion=d_congestion,
                t_dwell_core=a.core_dwell,
                t_dwell_loss=a.loss_dwell,
                d_signal_uniform_core=a.core_signal_uniform,
                d_signal_uniform_loss=a.loss_signal_uniform,
                d_signal_overflow_core=a.core_signal_overflow,
                d_signal_overflow_loss=a.loss_signal_overflow,
                d_crossing_core=a.core_crossing,
                d_crossing_loss=a.loss_crossing,
                t_dwell_near_signal=a.t_dwell_near_signal,
                attributions=a.attributions,
            )
        )

    return TripDecomp(trip_id=str(record["trip_id"]), segments=seg_decomps)


def aggregate_trips(decomps: list[TripDecomp]) -> pd.DataFrame:
    """One row per segment with mean/std across all decomposed trips."""
    if not decomps:
        return pd.DataFrame()
    seg_ids = [s.seg_id for s in decomps[0].segments]
    keys = (
        "t_obs", "t_ff", "t_dwell",
        "d_signal", "d_signal_uniform", "d_signal_overflow",
        "d_crossing", "d_congestion",
        "t_dwell_core", "t_dwell_loss",
        "d_signal_uniform_core", "d_signal_uniform_loss",
        "d_signal_overflow_core", "d_signal_overflow_loss",
        "d_crossing_core", "d_crossing_loss",
        "t_dwell_near_signal",
    )
    rows = []
    for i, seg_id in enumerate(seg_ids):
        col = {k: [] for k in keys}
        for d in decomps:
            s = d.segments[i]
            for k in keys:
                col[k].append(getattr(s, k))
        row = {"seg_id": seg_id, "n_trips": len(decomps)}
        for k, vs in col.items():
            arr = np.array(vs, dtype=float)
            row[f"mean_{k}"] = float(np.nanmean(arr)) if arr.size else float("nan")
            row[f"std_{k}"] = float(np.nanstd(arr)) if arr.size else float("nan")
        mean_dwell = row["mean_t_dwell"]
        row["t_dwell_near_signal_share"] = (
            row["mean_t_dwell_near_signal"] / mean_dwell if mean_dwell > 0 else 0.0
        )
        rows.append(row)
    return pd.DataFrame(rows)


def decompose_all_trips(
    trajectories_json: Path,
    segments: list[Segment],
    freeflow_t_obs: dict[str, float],
    **kwargs,
) -> list[TripDecomp]:
    """Convenience: decompose every trip in a serialized JSON bundle."""
    records = load_records(trajectories_json)
    out: list[TripDecomp] = []
    for rec in records:
        try:
            out.append(decompose_trip(rec, segments, freeflow_t_obs, **kwargs))
        except Exception as exc:
            print(f"  skipping trip {rec.get('trip_id')}: {exc}")
    return out
