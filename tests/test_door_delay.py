"""Shared door-delay classifier: the rules the network tab and the
single-trip speed view must agree on."""
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from core.decompose.door_delay import classify  # noqa: E402


def _trip(stops):
    """Trajectory that crawls through each (t_lo, t_hi) window and runs between."""
    t = np.arange(0.0, 1200.0, 1.0)
    v = np.full_like(t, 9.0)                    # ~20 mph between stops
    for lo, hi in stops:
        v[(t >= lo) & (t < hi)] = 0.0
    return t, np.concatenate([[0.0], np.cumsum(v[:-1])])


def test_event_without_door_is_nd():
    t, x = _trip([(300, 360)])                  # 60 s halt, no door
    got = classify(t, x, doors=[])
    assert [p.cls for p in got] == ["nd"]
    assert got[0].duration_s > 50


def test_pre_and_post_shoulders_need_ten_seconds():
    t, x = _trip([(300, 400)])
    # doors open 40 s into the halt, close 30 s before it ends
    got = classify(t, x, doors=[[340, 370]], stop_ids=["s1"])
    cls = {p.cls for p in got}
    assert "pre" in cls and "post" in cls and "dw" in cls
    pre = next(p for p in got if p.cls == "pre")
    post = next(p for p in got if p.cls == "post")
    assert pre.duration_s > 10 and post.duration_s > 10
    assert pre.stop_id == "s1"


def test_shoulder_under_ten_seconds_is_dropped():
    t, x = _trip([(300, 400)])
    # opens 5 s in, closes 5 s before the end: neither shoulder qualifies
    got = classify(t, x, doors=[[305, 395]], stop_ids=["s1"])
    assert {p.cls for p in got} == {"dw"}


def test_two_cycles_in_one_event_give_post2():
    t, x = _trip([(300, 460)])
    got = classify(t, x, doors=[[320, 340], [360, 380]], stop_ids=["a", "b"])
    assert any(p.cls == "post2" for p in got)
    assert not any(p.cls == "post" for p in got)


def test_straddling_cycle_yields_no_shoulder_on_that_side():
    t, x = _trip([(300, 400)])
    # cycle opens BEFORE the event starts -> no pre, but post still forms
    got = classify(t, x, doors=[[280, 330]], stop_ids=["s1"])
    cls = [p.cls for p in got]
    assert "pre" not in cls and "post" in cls


def test_quick_stop_without_event_still_counts_as_dwell():
    t, x = _trip([])                            # never slows below 5 mph
    got = classify(t, x, doors=[[100, 108]], stop_ids=["s1"])
    assert [p.cls for p in got] == ["dw"]


def test_near_side_post_gets_its_own_render_class():
    t, x = _trip([(300, 400)])
    got = classify(t, x, doors=[[340, 370]], stop_ids=["s1"],
                   near_side={"s1"}, stop_names={"s1": "Main & 1st"})
    post = next(p for p in got if p.cls == "post")
    assert post.near_side and post.render_cls == "post_ns"
    assert post.stop_name == "Main & 1st"
    pre = next(p for p in got if p.cls == "pre")
    assert pre.render_cls == "pre"          # only post carries the combo


def test_far_side_post_is_plain_purple():
    t, x = _trip([(300, 400)])
    got = classify(t, x, doors=[[340, 370]], stop_ids=["s1"], near_side=set())
    post = next(p for p in got if p.cls == "post")
    assert not post.near_side and post.render_cls == "post"


def test_dwell_blob_merges_touching_cycles():
    t, x = _trip([(300, 500)])
    got = classify(t, x, doors=[[320, 360], [355, 400]], stop_ids=["a", "b"])
    dw = [p for p in got if p.cls == "dw"]
    assert len(dw) == 1                      # merged, not double counted
    assert dw[0].t_start <= 320 and dw[0].t_end >= 400


def test_short_shoulders_returned_as_red_when_requested():
    """The speed view needs a continuous bar; the pipeline does not."""
    t, x = _trip([(300, 400)])
    doors = [[305, 395]]                     # both shoulders only ~5 s
    assert {p.cls for p in classify(t, x, doors, stop_ids=["s1"])} == {"dw"}
    got = classify(t, x, doors, stop_ids=["s1"], emit_short_shoulders=True)
    reds = sorted((p for p in got if p.cls == "nd"), key=lambda p: p.t_start)
    assert len(reds) == 2 and all(p.duration_s <= 10 for p in reds)
    # red + door + red tile the dwell union exactly — the speed view draws
    # them as one continuous bar, so no gaps and no overlap.
    dw = next(p for p in got if p.cls == "dw")
    o, c = doors[0]
    assert reds[0].t_start == dw.t_start and reds[0].t_end == o
    assert reds[1].t_start == c and reds[1].t_end == dw.t_end


def test_sanitize_strips_terminal_layover_dwell():
    """First/last event of a trip carries the layover, not door time."""
    from core.decompose.door_delay import sanitize_cycles
    cycles = [
        {"open": 0, "close": 832, "trip_key": "A"},      # last of trip A
        {"open": 300, "close": 1132, "trip_key": "B"},   # first of trip B
        {"open": 430, "close": 438, "trip_key": "B"},    # a real stop
        {"open": 520, "close": 526, "trip_key": "B"},
    ]
    got = sanitize_cycles(cycles)
    assert got[0]["close"] == got[0]["open"]          # layover dropped
    assert got[1]["close"] == got[1]["open"]
    assert got[2]["close"] - got[2]["open"] == 8      # real dwell untouched
    # and nothing overlaps its successor any more
    assert all(a["close"] <= b["open"] + 1e-9 for a, b in zip(got, got[1:]))


def test_sanitize_keeps_long_mid_trip_dwell():
    """A genuine hold in the middle of a trip is not a layover."""
    from core.decompose.door_delay import sanitize_cycles
    cycles = [
        {"open": 0, "close": 10, "trip_key": "A"},
        {"open": 100, "close": 400, "trip_key": "A"},   # 300 s hold, mid-trip
        {"open": 600, "close": 610, "trip_key": "A"},
    ]
    got = sanitize_cycles(cycles)
    assert got[1]["close"] - got[1]["open"] == 300
