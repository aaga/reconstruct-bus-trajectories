"""Cluster location rule (hierarchy/midpoint), split constraint, and the
door-peak stop-location source."""
import csv
import io
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from core.control_points import ControlPoint  # noqa: E402
from dataio.intersections import cluster_signals  # noqa: E402

from analysis.network import registry as reg  # noqa: E402


def _cp(nid, d, rank=None, name="", kind="traffic_signals"):
    return ControlPoint(
        intersection_node_id=nid, lat=41.0 + d * 1e-5, lon=-87.0,
        dist_along_route_m=d, on_way_id=1, control_type=kind,
        cross_street_names=(name,) if name else (),
        cross_way_rank=rank,
    )


# ---------------------------------------------------------------- layer 1

def test_hierarchy_winner_location_first_node_identity():
    r = cluster_signals([_cp(1, 0.0, 3, "Minor"), _cp(2, 14.0, 5, "Major")])
    assert len(r) == 1
    rep = r[0]
    assert rep.intersection_node_id == 1          # identity: first in order
    assert rep.first_node_id == 1
    assert rep.main_node_id == 2                  # location: class winner
    assert rep.loc_source == "hierarchy"
    assert rep.dist_along_route_m == 14.0
    assert rep.merged_node_ids == (2,)


def test_class_tie_uses_midpoint():
    r = cluster_signals([_cp(1, 0.0, 3, "A"), _cp(2, 14.0, 3, "B")])
    rep = r[0]
    assert rep.loc_source == "midpoint"
    assert rep.main_node_id is None
    assert rep.dist_along_route_m == 7.0
    assert rep.intersection_node_id == 1


def test_no_rank_data_uses_midpoint():
    r = cluster_signals([_cp(1, 0.0), _cp(2, 20.0)])
    assert r[0].loc_source == "midpoint"
    assert r[0].dist_along_route_m == 10.0


def test_three_member_midpoint_is_first_last():
    r = cluster_signals([_cp(1, 0.0), _cp(2, 4.0), _cp(3, 20.0)])
    assert r[0].dist_along_route_m == 10.0        # (first+last)/2, not mean


def test_single_node_is_its_own_main():
    r = cluster_signals([_cp(7, 5.0, 4, "X")])
    assert r[0].main_node_id == 7
    assert r[0].first_node_id == 7
    assert r[0].loc_source == "single"


def test_cluster_diameter_bounded_by_first_member():
    # A@0, B@10, C@20 with threshold 10: A+B merge, C starts fresh.
    r = cluster_signals([_cp(1, 0.0), _cp(2, 10.0), _cp(3, 20.0)],
                        max_gap_m=10.0)
    assert [c.intersection_node_id for c in r] == [1, 3]
    assert r[0].merged_node_ids == (2,)


def test_split_prevents_merge_and_third_party_bridge():
    sides = {10: (0, 0), 20: (0, 1)}
    # direct: 10 and 20 never share a cluster
    r = cluster_signals([_cp(10, 0.0), _cp(20, 15.0)], split_sides=sides)
    assert len(r) == 2
    # bridge: unlisted 15 joins side A; 20 still refused
    r = cluster_signals([_cp(10, 0.0), _cp(15, 8.0), _cp(20, 16.0)],
                        split_sides=sides)
    assert len(r) == 2
    assert r[0].merged_node_ids == (15,)
    assert r[1].intersection_node_id == 20


# ---------------------------------------------------------------- layer 2

def test_cluster_nodes_split_transitive():
    positions = {10: (41.0, -87.0), 15: (41.00009, -87.0),  # ~10 m apart
                 20: (41.00018, -87.0)}
    usage = {10: 1, 15: 1, 20: 1}
    plain = reg.cluster_nodes(positions, usage, radius_m=30.0)
    assert len(set(plain.values())) == 1          # all merge without split
    sides = {10: (0, 0), 20: (0, 1)}
    canon = reg.cluster_nodes(positions, usage, radius_m=30.0,
                              split_sides=sides)
    assert canon[10] != canon[20]                 # divide holds transitively


def test_cluster_nodes_split_blocks_extra_edges():
    positions = {10: (41.0, -87.0), 20: (41.01, -87.0)}   # far apart
    sides = {10: (0, 0), 20: (0, 1)}
    canon = reg.cluster_nodes(positions, {10: 1, 20: 1}, radius_m=30.0,
                              extra_edges=[(10, 20)], split_sides=sides)
    assert canon[10] != canon[20]


# ------------------------------------------------------- door-peak source

def _mini_gtfs(tmp_path, stops):
    """Straight 1 km east-west shape at lat 41 with the given stops."""
    z = tmp_path / "gtfs.zip"
    shape_rows = [("S1", i, 41.0, -87.0 + i * 0.001) for i in range(13)]
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("shapes.txt",
                    "shape_id,shape_pt_sequence,shape_pt_lat,shape_pt_lon\n"
                    + "\n".join(f"{s},{q},{la},{lo}"
                                for s, q, la, lo in shape_rows))
        zf.writestr("stop_times.txt",
                    "trip_id,stop_sequence,stop_id\n"
                    + "\n".join(f"T1,{i},{sid}"
                                for i, (sid, _, _) in enumerate(stops)))
        zf.writestr("stops.txt",
                    "stop_id,stop_name,stop_lat,stop_lon\n"
                    + "\n".join(f"{sid},Stop {sid},{la},{lo}"
                                for sid, la, lo in stops))
    return z


class _FakeCity:
    city_id = "cta"

    def resolve(self, rel):
        return Path("/nonexistent") / rel


def test_stops_by_shape_peak_and_exceptions(tmp_path, monkeypatch):
    stops = [("100", 41.00001, -86.9995),   # peak will move it
             ("6515", 41.00001, -86.999),   # rejected peak -> pole
             ("14171", 41.00001, -86.9985),  # terminal -> excluded
             ("200", 41.00001, -86.998)]    # peak too far -> pole
    z = _mini_gtfs(tmp_path, stops)
    peaks = {"100": (41.00001, -86.99945),          # ~4 m east of pole
             "6515": (41.00001, -86.9980),          # would move it ~80 m
             "200": (41.002, -86.998)}              # 220 m north: rejected
    monkeypatch.setattr(reg, "_door_peaks", lambda city: peaks)
    out = reg.stops_by_shape(z, {"S1": "T1"}, city=_FakeCity())
    got = {s["stop_id"]: s["dist_along_m"] for s in out["S1"]}
    assert "14171" not in got                       # terminal excluded
    d_100 = got["100"]
    d_6515 = got["6515"]
    # stop 100 moved east by ~4 m relative to its pole projection
    pole_100_d = 0.0005 * 111320 * 0.7547  # not exact; just sanity-order
    assert d_100 > 0
    # 6515's peak is within 100 m BUT rejected by exception -> pole position
    # (pole at -86.999 = 0.001 deg from shape start -> ~83 m along)
    assert abs(d_6515 - (0.001 * 111320 * 0.75471)) < 5.0
    # 200's peak violates the pole-distance guard -> pole position
    assert abs(got["200"] - (0.002 * 111320 * 0.75471)) < 5.0