"""Tests for the delay_decomposition package."""

from __future__ import annotations

import numpy as np
import pytest

from bus_trajectories.delay_decomposition import (
    AbsoluteSpeedThreshold,
    AVLDwellAttributor,
    Event,
    EventAttribution,
    ProximityDwellAttributor,
    Segment,
    StopOnRoute,
    attribute_event,
    decompose_trip,
    detect_events,
)
from bus_trajectories.delay_decomposition.decompose import _evaluate_dense  # noqa
from bus_trajectories.delay_decomposition.dwell import _clip_zone
from bus_trajectories.delay_decomposition.segments import (
    build_segments_from_records,
)
from bus_trajectories.intersections import (
    ControlPoint,
    classify_near_side_stops,
)


# ---------- helpers -------------------------------------------------------


def _cp(node_id: int, x: float, control_type: str) -> ControlPoint:
    return ControlPoint(
        intersection_node_id=node_id,
        lat=0.0,
        lon=0.0,
        dist_along_route_m=float(x),
        on_way_id=0,
        control_type=control_type,
        cross_street_names=(),
        merged_node_ids=(),
        anchor_intersection_node_id=None,
        signalized=(control_type in {"traffic_signals", "ped_crossing_signal"}),
        markings="",
        has_island=False,
    )


def _stop(stop_id: str, x: float, *, name: str = "test stop") -> dict:
    return {"stop_id": stop_id, "name": name, "dist_along_m": float(x)}


# ---------- classify_near_side_stops -------------------------------------


def test_classify_near_side_stops_within_threshold():
    stops = [_stop("S1", 200.0)]
    signals = [_cp(1, 215.0, "traffic_signals")]  # 15 m downstream of stop
    assert classify_near_side_stops(stops, signals, threshold_m=30) == {"S1"}


def test_classify_near_side_stops_beyond_threshold():
    stops = [_stop("S1", 200.0)]
    signals = [_cp(1, 240.0, "traffic_signals")]  # 40 m downstream → too far
    assert classify_near_side_stops(stops, signals, threshold_m=30) == set()


def test_classify_near_side_stops_signal_upstream():
    stops = [_stop("S1", 200.0)]
    signals = [_cp(1, 180.0, "traffic_signals")]  # upstream → not near-side
    assert classify_near_side_stops(stops, signals, threshold_m=30) == set()


def test_classify_near_side_stops_mid_block_ped_signal_counts():
    stops = [_stop("S1", 200.0)]
    signals = [_cp(1, 215.0, "ped_crossing_signal")]  # mid-block ped sig counts
    assert classify_near_side_stops(stops, signals, threshold_m=30) == {"S1"}


def test_classify_near_side_stops_non_signalized_crossing_ignored():
    stops = [_stop("S1", 200.0)]
    signals = [_cp(1, 215.0, "ped_crossing_marked")]
    assert classify_near_side_stops(stops, signals, threshold_m=30) == set()


# ---------- build_segments -----------------------------------------------


def test_build_segments_signal_to_signal():
    cps = [
        _cp(1, 0.0, "traffic_signals"),
        _cp(2, 200.0, "ped_crossing_marked"),  # non-signalized
        _cp(3, 500.0, "ped_crossing_signal"),  # signalized ped (mid-block)
        _cp(4, 800.0, "traffic_signals"),
    ]
    stops = [_stop("S1", 100.0), _stop("S2", 350.0), _stop("S3", 650.0)]
    segs = build_segments_from_records(cps, stops)
    # 3 signalized → 2 segments
    assert len(segs) == 2
    assert segs[0].upstream_signal.intersection_node_id == 1
    assert segs[0].downstream_signal.intersection_node_id == 3
    assert segs[1].upstream_signal.intersection_node_id == 3
    assert segs[1].downstream_signal.intersection_node_id == 4
    # crossing within first segment
    assert len(segs[0].crossings) == 1
    assert segs[0].crossings[0].intersection_node_id == 2


def test_build_segments_mid_block_ped_signal_boundary():
    cps = [
        _cp(1, 0.0, "traffic_signals"),
        _cp(2, 500.0, "ped_crossing_signal"),
        _cp(3, 1000.0, "traffic_signals"),
    ]
    segs = build_segments_from_records(cps, [])
    assert len(segs) == 2
    assert segs[0].x_end_m == 500.0
    assert segs[1].x_start_m == 500.0


# ---------- dwell zone clipping ------------------------------------------


def test_proximity_dwell_zone_clipping_back():
    cps = [
        _cp(1, 0.0, "traffic_signals"),
        _cp(2, 85.0, "ped_crossing_marked"),  # interferes with stop's back
        _cp(3, 300.0, "traffic_signals"),
    ]
    stops = [_stop("S1", 100.0)]
    segs = build_segments_from_records(cps, stops)
    seg = segs[0]
    zone_lo, zone_hi = _clip_zone(100.0, 30.0, 10.0, seg)
    assert zone_lo == pytest.approx(85.0)  # clipped at intersection
    assert zone_hi == pytest.approx(110.0)  # unclipped


def test_proximity_dwell_zone_clipping_ahead():
    cps = [
        _cp(1, 0.0, "traffic_signals"),
        _cp(2, 105.0, "ped_crossing_marked"),  # interferes with stop's front
        _cp(3, 300.0, "traffic_signals"),
    ]
    stops = [_stop("S1", 100.0)]
    segs = build_segments_from_records(cps, stops)
    zone_lo, zone_hi = _clip_zone(100.0, 30.0, 10.0, segs[0])
    assert zone_lo == pytest.approx(70.0)
    assert zone_hi == pytest.approx(105.0)


# ---------- event detection ----------------------------------------------


def test_event_detection_threshold_default():
    t = np.linspace(0, 60, 601)
    # Speed = 20 mph, dips to 2 mph from t=20..30, back to 20.
    v_mph = np.full_like(t, 20.0)
    v_mph[(t >= 20) & (t <= 30)] = 2.0
    x = np.cumsum(v_mph * (t[1] - t[0]) * 0.44704)  # rough distance
    events = detect_events(t, x, v_mph, AbsoluteSpeedThreshold(5.0), min_duration_s=2)
    assert len(events) == 1
    assert events[0].t_start == pytest.approx(20.0, abs=0.5)
    assert events[0].t_end == pytest.approx(30.0, abs=0.5)


def test_event_detection_threshold_swap():
    class HighThreshold:
        def __call__(self, t, x, v):
            return v < 15.0
    t = np.linspace(0, 60, 601)
    v_mph = np.full_like(t, 10.0)  # always 10 mph
    x = np.cumsum(v_mph * (t[1] - t[0]) * 0.44704)
    abs5 = detect_events(t, x, v_mph, AbsoluteSpeedThreshold(5.0), min_duration_s=2)
    abs15 = detect_events(t, x, v_mph, HighThreshold(), min_duration_s=2)
    assert len(abs5) == 0  # 10 mph not below 5
    assert len(abs15) == 1  # 10 mph is below 15


# ---------- attribute_event ----------------------------------------------


def test_attribute_event_dwell_wins_over_signal():
    cps = [
        _cp(1, 0.0, "traffic_signals"),
        _cp(2, 300.0, "traffic_signals"),
    ]
    stops = [_stop("S1", 250.0)]  # 50 m upstream of downstream signal
    segs = build_segments_from_records(cps, stops, near_side_threshold_m=80.0)
    seg = segs[0]
    # Event right at the stop's zone — overlaps both stop AND signal-approach.
    event = Event(
        t_start=100, t_end=140, x_start=240.0, x_end=255.0, min_v_mph=2.0
    )
    attr = attribute_event(event, seg, ProximityDwellAttributor(), loss_s=3.0)
    assert attr.category == "dwell"
    assert attr.dwell_near_signal is True  # 50 m < 80 m threshold


def test_attribute_event_signal_uniform_when_no_stop():
    cps = [
        _cp(1, 0.0, "traffic_signals"),
        _cp(2, 300.0, "traffic_signals"),
    ]
    segs = build_segments_from_records(cps, [])
    seg = segs[0]
    event = Event(
        t_start=100, t_end=120, x_start=280.0, x_end=298.0, min_v_mph=2.0
    )
    attr = attribute_event(event, seg, ProximityDwellAttributor(), loss_s=2.0)
    assert attr.category == "signal_uniform"
    assert attr.facility_id == "SIG_2"


def test_attribute_event_slowdown_fallback():
    cps = [
        _cp(1, 0.0, "traffic_signals"),
        _cp(2, 1000.0, "traffic_signals"),
    ]
    segs = build_segments_from_records(cps, [])
    # Event in the middle, > 91.44 m (300 ft) upstream of downstream signal.
    event = Event(t_start=200, t_end=220, x_start=400.0, x_end=420.0, min_v_mph=3.0)
    attr = attribute_event(event, segs[0], ProximityDwellAttributor(), loss_s=1.0)
    assert attr.category == "slowdown"


def test_overflow_pass_converts_preceding_slowdown():
    """A slowdown immediately preceding a signal_uniform (same primary
    segment, no dwell/crossing in between) should be re-labeled
    signal_overflow."""
    from bus_trajectories.delay_decomposition.decompose import _apply_overflow_pass

    cps = [
        _cp(1, 0.0, "traffic_signals"),
        _cp(2, 1000.0, "traffic_signals"),
    ]
    segs = build_segments_from_records(cps, [])
    seg = segs[0]
    slow = Event(t_start=100, t_end=130, x_start=600.0, x_end=620.0, min_v_mph=3.0)
    sig = Event(t_start=150, t_end=200, x_start=950.0, x_end=998.0, min_v_mph=0.0)
    slow_attr = EventAttribution(
        event=slow, category="slowdown", facility_id=None,
        core_s=30.0, loss_s=0.0,
    )
    sig_attr = EventAttribution(
        event=sig, category="signal_uniform", facility_id="SIG_2",
        core_s=50.0, loss_s=0.0,
    )
    records = [(slow, slow_attr, seg), (sig, sig_attr, seg)]
    out = _apply_overflow_pass(records)
    assert out[0][1].category == "signal_overflow"
    assert out[0][1].facility_id == "SIG_2"
    assert out[1][1].category == "signal_uniform"


def test_overflow_pass_stops_at_dwell():
    """A dwell between slowdown and signal_uniform should block overflow."""
    from bus_trajectories.delay_decomposition.decompose import _apply_overflow_pass

    cps = [
        _cp(1, 0.0, "traffic_signals"),
        _cp(2, 1000.0, "traffic_signals"),
    ]
    segs = build_segments_from_records(cps, [])
    seg = segs[0]
    slow = Event(t_start=100, t_end=130, x_start=400.0, x_end=420.0, min_v_mph=3.0)
    dwell = Event(t_start=140, t_end=170, x_start=500.0, x_end=520.0, min_v_mph=0.0)
    sig = Event(t_start=180, t_end=220, x_start=950.0, x_end=998.0, min_v_mph=0.0)
    records = [
        (slow, EventAttribution(event=slow, category="slowdown",
                                  facility_id=None, core_s=30, loss_s=0), seg),
        (dwell, EventAttribution(event=dwell, category="dwell",
                                   facility_id="S1", core_s=30, loss_s=0), seg),
        (sig, EventAttribution(event=sig, category="signal_uniform",
                                 facility_id="SIG_2", core_s=40, loss_s=0), seg),
    ]
    out = _apply_overflow_pass(records)
    # slow stays slowdown — dwell blocked the walk-back.
    assert out[0][1].category == "slowdown"


# ---------- AVL stub -----------------------------------------------------


def test_avl_dwell_attributor_is_not_implemented():
    attr = AVLDwellAttributor()
    cps = [_cp(1, 0.0, "traffic_signals"), _cp(2, 200.0, "traffic_signals")]
    segs = build_segments_from_records(cps, [])
    event = Event(t_start=0, t_end=10, x_start=50, x_end=60, min_v_mph=0)
    with pytest.raises(NotImplementedError, match="AVL"):
        attr.attribute(event, segs[0])


# ---------- decompose_trip identity (synthetic) --------------------------


def test_decompose_trip_sum_closes():
    """T_obs == T_ff + T_dwell + D_signal + D_crossing + D_congestion."""
    # Build a synthetic PCHIP record: 1000 m route at constant 5 m/s with a
    # 20 s dwell from t=100..120 at x=500.
    t_knots = np.array([0., 99., 100., 120., 121., 300.])
    x_knots = np.array([0., 495., 500., 500., 505., 1395.])
    slopes = np.array([5., 5., 0., 0., 5., 5.])
    rec = {
        "trip_id": "test1",
        "t_knots": t_knots.tolist(),
        "x_knots": x_knots.tolist(),
        "slopes": slopes.tolist(),
    }
    cps = [_cp(1, 0.0, "traffic_signals"), _cp(2, 1000.0, "traffic_signals")]
    stops = [_stop("STOP1", 500.0)]
    segs = build_segments_from_records(cps, stops)
    ff = {segs[0].seg_id: 100.0}  # somewhat shorter than observed
    d = decompose_trip(rec, segs, ff)
    s = d.segments[0]
    closed = s.t_ff + s.t_dwell + s.d_signal + s.d_crossing + s.d_congestion
    assert s.t_obs == pytest.approx(closed, abs=1.0)
    # Stop activity at x=500 should land in dwell.
    assert s.t_dwell > 10.0
