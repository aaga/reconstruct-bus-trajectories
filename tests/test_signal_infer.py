"""Crosswalk-based signal inference + signal_add/signal_reject (signal_infer)."""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from analysis.network.signal_infer import apply, is_signalized_crossing  # noqa: E402
from core.control_points import ControlPoint  # noqa: E402

LAT, LON = 41.868759, -71.39859
M = 1 / 110540.0  # degrees latitude per metre


def _j(node, dlat_m=0.0, along=100.0, ctype="uncontrolled_junction"):
    return ControlPoint(intersection_node_id=node, lat=LAT + dlat_m * M, lon=LON,
                        dist_along_route_m=along, on_way_id=1,
                        control_type=ctype, cross_street_names=("X St",),
                        signalized=ctype == "traffic_signals")


def _ped(node, along):
    return ControlPoint(intersection_node_id=node, lat=LAT, lon=LON,
                        dist_along_route_m=along, on_way_id=1,
                        control_type="ped_crossing_signal", cross_street_names=(),
                        signalized=True)


def _xing(dn_m, de_m):
    return (LAT + dn_m * M, LON + de_m * M / 0.745)


def _run(cps, xings, signals=(), adds=(), rejects=()):
    out, rep = apply({"s": cps}, xings=list(xings), signals=list(signals),
                     infer=True, adds=frozenset(adds), rejects=frozenset(rejects))
    return out["s"], rep


def test_two_legs_promote_and_drop_nearby_ped_signal():
    cps, rep = _run([_j(1), _ped(9, 110)], [_xing(12, 0), _xing(0, 12)])
    assert [c.control_type for c in cps] == ["traffic_signals"]
    assert rep[0]["source"] == "crosswalks:2_legs"


def test_one_leg_is_not_enough():
    cps, _ = _run([_j(1)], [_xing(12, 0), _xing(15, 3)])  # same leg, ~14 deg apart
    assert cps[0].control_type == "uncontrolled_junction"


def test_osm_signal_nearby_blocks_inference():
    cps, _ = _run([_j(1)], [_xing(12, 0), _xing(0, 12)], signals=[_xing(30, 0)])
    assert cps[0].control_type == "uncontrolled_junction"


def test_reject_blocks_and_demotes():
    cps, _ = _run([_j(1), _j(2, 500, 600, "traffic_signals")],
                  [_xing(12, 0), _xing(0, 12)], rejects={1, 2})
    assert [c.control_type for c in cps] == ["uncontrolled_junction"] * 2


def test_add_without_evidence_and_promoted_neighbours_cluster():
    cps, rep = _run([_j(1, 0, 100), _j(2, 20, 120)], [], adds={1, 2})
    assert len(cps) == 1 and cps[0].control_type == "traffic_signals"
    assert cps[0].merged_node_ids == (2,)
    assert {r["source"] for r in rep} == {"signal_add"}


def test_idempotent():
    cps, _ = _run([_j(1)], [_xing(12, 0), _xing(0, 12)])
    again, rep = _run(cps, [_xing(12, 0), _xing(0, 12)])
    assert again == cps and rep == []


def test_crossing_tag_variants():
    assert is_signalized_crossing({"highway": "crossing", "crossing": "traffic_signals"})
    assert is_signalized_crossing({"highway": "crossing", "crossing:signals": "yes"})
    assert not is_signalized_crossing({"highway": "crossing", "crossing:signals": "no",
                                       "crossing": "uncontrolled"})
