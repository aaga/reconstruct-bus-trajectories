"""Reconstruct + decompose one trip's ping set at any cadence.

Reuses the repo pipeline end-to-end: SnapToShape map-matching,
LOCREG-PCHIP smoothing (``core.reconstruct.reconstruct_trip``), slowdown
detection and delay attribution (``core.decompose.decompose_trip``) on the
signal-to-signal segments from the corridor's OSM intersection cache
(``analysis.comparison.shape_bundle``).

The only knob that varies with cadence is the LOCREG bandwidth, which is a
k-NN count: we hold the LOCREG *time window* constant (~120 s, the repo's
convention) and derive k from the dataset's median ping interval.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

import config as C  # noqa: E402

from analysis.comparison import matcher_for, shape_bundle  # noqa: E402
from core.decompose.decompose import decompose_trip  # noqa: E402
from core.decompose.events import detect_events, AbsoluteSpeedThreshold  # noqa: E402
from core.decompose.segments import build_segments_from_records  # noqa: E402
from core.reconstruct import reconstruct_trip  # noqa: E402
from core.serialize import to_pchip_record  # noqa: E402

_NO_FREEFLOW: dict[str, float] = {}

# ------------------------------------------------------------- shape caches

_MATCHERS: dict[str, object] = {}
_BUNDLES: dict[str, tuple] = {}


def matcher(shape_id: str):
    if shape_id not in _MATCHERS:
        _MATCHERS[shape_id] = matcher_for(shape_id)
    return _MATCHERS[shape_id]


def bundle(shape_id: str) -> tuple:
    """(segments, stops_raw, facility_labels) for a shape, cached."""
    if shape_id not in _BUNDLES:
        _shape, _features, control_points, stops_raw, facility_labels = shape_bundle(shape_id)
        segments = build_segments_from_records(control_points, stops_raw)
        _BUNDLES[shape_id] = (segments, stops_raw, facility_labels)
    return _BUNDLES[shape_id]


# ------------------------------------------------------------- downsampling

def downsample(pings: pd.DataFrame, level: int) -> pd.DataFrame:
    """Keep every ``2**level``-th ping (level 0 = benchmark, untouched).

    Equivalent to applying "drop every other ping" ``level`` times, so the
    ladder is exactly the successive-halving design.
    """
    return pings.iloc[:: 2 ** level].reset_index(drop=True)


# ------------------------------------------------------------- reconstruction

@dataclass
class LevelResult:
    level: int
    cadence_s: float          # measured median ping interval
    bandwidth: int
    n_pings: int
    n_on_route: int
    t: np.ndarray             # dense 1 Hz grid, seconds since the trip's t0
    x: np.ndarray             # meters along shape
    v_mph: np.ndarray
    events: list              # detected slowdown Events (t in trip seconds)
    attributions: list[dict]  # {t_start, t_end, category, facility_id}
    seg_buckets: list[dict]   # per signal-to-signal segment delay buckets:
                              # {seg_id, x_start_m, x_end_m, t_dwell_s, d_signal_s}


def run_level(trip, level: int) -> LevelResult:
    """Reconstruct + decompose one downsampling level of one trip.

    Raises ValueError when the ping set is too sparse to reconstruct — the
    caller records that as a reconstruction failure for the level.
    """
    pings = downsample(trip.pings, level)
    dt = pings["ping_dt"].diff().dt.total_seconds().iloc[1:]
    cadence = float(dt.median()) if len(dt) else float("nan")
    bw = C.bandwidth_for_cadence(cadence if np.isfinite(cadence) else C.level_cadence_s(level))

    trip_df = pd.DataFrame({
        "trip_id": trip.trip_key,
        "bus_id": trip.bus_id,
        "route_id": trip.route_id,
        "pattern_id": trip.shape_id[3:],
        "avl_event_time": pings["ping_dt"],
        "latitude": pings["latitude"].astype(float),
        "longitude": pings["longitude"].astype(float),
    })
    recon = reconstruct_trip(trip_df, matcher(trip.shape_id), bandwidth=bw)

    # NOTE: reconstruct_trip anchors t=0 at the first ping of trip_df.
    # downsample() always keeps index 0, so t=0 is the same wall instant
    # (trip.t0) at every level and time axes align across levels.
    record = to_pchip_record(recon)

    # Dense 1 Hz grid over the reconstruction's domain.
    f = recon.smoothed.f
    t_lo, t_hi = float(f.x[0]), float(f.x[-1])
    t = np.arange(np.ceil(t_lo), np.floor(t_hi) + 1e-9, C.DENSE_DT_S)
    x = np.asarray(f(t), dtype=float)
    v_mph = np.clip(np.asarray(f.derivative()(t), dtype=float) * C.MPS_TO_MPH, 0, None)

    # Slowdown events straight from the pipeline's detector (same grid).
    events = detect_events(t, x, v_mph, AbsoluteSpeedThreshold(C.SLOW_MPH),
                           min_duration_s=C.MIN_EVENT_S)

    # Delay attribution via the full decomposition stack.
    segments, _stops, _labels = bundle(trip.shape_id)
    attributions: list[dict] = []
    seg_buckets: list[dict] = []
    if segments:
        decomp = decompose_trip(record, segments, _NO_FREEFLOW,
                                dense_dt_s=C.DENSE_DT_S,
                                min_duration_s=C.MIN_EVENT_S)
        for seg, sd in zip(segments, decomp.segments):
            for a in sd.attributions:
                attributions.append({
                    "t_start": float(a.event.t_start),
                    "t_end": float(a.event.t_end),
                    "category": a.category,
                    "facility_id": a.facility_id,
                    "dwell_near_signal": bool(a.dwell_near_signal),
                })
            seg_buckets.append({
                "seg_id": sd.seg_id,
                "x_start_m": float(seg.x_start_m),
                "x_end_m": float(seg.x_end_m),
                "t_dwell_s": float(sd.t_dwell),
                "d_signal_s": float(sd.d_signal),
            })

    return LevelResult(
        level=level,
        cadence_s=cadence,
        bandwidth=bw,
        n_pings=len(pings),
        n_on_route=int(recon.meta.n_on_route),
        t=t, x=x, v_mph=v_mph,
        events=events,
        attributions=attributions,
        seg_buckets=seg_buckets,
    )
