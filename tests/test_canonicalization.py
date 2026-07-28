"""Node clustering, cluster-dedupe, and the canonical traversal view."""

from __future__ import annotations

import json

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from analysis.network.registry import _dedupe_by_cluster, cluster_nodes
from analysis.network.traversals_view import create_canonical_view
from dataio.cities import get_city


# --------------------------------------------------------------------------
# cluster_nodes
# --------------------------------------------------------------------------

def test_cluster_nodes_merges_within_radius():
    # Two nodes 15 m apart (aliases) + one 100 m away.
    positions = {
        1: (41.8000, -87.7000),
        2: (41.80013, -87.7000),  # ~14.5 m north
        3: (41.8009, -87.7000),   # ~100 m north
    }
    usage = {1: 5, 2: 2, 3: 7}
    canon = cluster_nodes(positions, usage, radius_m=30.0)
    assert canon[1] == canon[2] == 1  # rep = most-used (node 1, 5 shapes)
    assert canon[3] == 3  # too far — own cluster


def test_cluster_nodes_rep_tiebreak_lowest_id():
    positions = {10: (41.8, -87.7), 7: (41.80005, -87.7)}
    usage = {10: 3, 7: 3}
    canon = cluster_nodes(positions, usage, radius_m=30.0)
    assert canon[10] == canon[7] == 7


def test_cluster_nodes_chained_clusters_union():
    # A-B 20 m, B-C 20 m, A-C 40 m: transitive union puts all three together.
    positions = {
        1: (41.8000, -87.7),
        2: (41.80018, -87.7),
        3: (41.80036, -87.7),
    }
    canon = cluster_nodes(positions, {1: 1, 2: 9, 3: 1}, radius_m=30.0)
    assert len({canon[1], canon[2], canon[3]}) == 1
    assert canon[1] == 2  # most used


# --------------------------------------------------------------------------
# _dedupe_by_cluster
# --------------------------------------------------------------------------

class _CP:
    def __init__(self, node, x):
        self.intersection_node_id = node
        self.dist_along_route_m = x


def test_dedupe_keeps_first_of_each_run():
    canon = {1: 1, 2: 1, 3: 3, 4: 3, 5: 5}
    sigs = [_CP(1, 0), _CP(2, 15), _CP(3, 300), _CP(4, 318), _CP(5, 700)]
    out = _dedupe_by_cluster(sigs, canon)
    assert [s.intersection_node_id for s in out] == [1, 3, 5]


# --------------------------------------------------------------------------
# canonical traversal view: exact merge + coverage rule
# --------------------------------------------------------------------------

@pytest.fixture()
def synthetic(tmp_path):
    """Two old segs A->P, P->B on shape S1 mapping to canonical A->B."""
    rows = []
    t0 = pd.Timestamp("2026-05-05 13:00:00", tz="UTC")

    def row(trip, seg, enter_s, exit_s, flags=0):
        return {
            "seg_id": seg, "route_id": "22", "shape_id": "S1",
            "direction": "South", "service_date": pd.Timestamp("2026-05-05").date(),
            "trip_key": trip, "vehicle_id": "v1",
            "t_enter_utc": t0 + pd.Timedelta(seconds=enter_s),
            "t_exit_utc": t0 + pd.Timedelta(seconds=exit_s),
            "t_obs_s": float(exit_s - enter_s), "seg_len_m": 200.0,
            "n_pings_in_seg": 3, "max_gap_in_seg_s": 20.0,
            "hour_local": 8, "period": "am_peak", "flags": flags,
        }

    # trip1: full coverage (both constituents, contiguous at t=+40)
    rows += [row("trip1", "SIG_A__SIG_P", 0, 40), row("trip1", "SIG_P__SIG_B", 40, 95)]
    # trip2: partial coverage (only first constituent) -> excluded
    rows += [row("trip2", "SIG_A__SIG_P", 0, 38)]
    # trip3: full, one part terminal-flagged -> merged flags carry the bit
    rows += [row("trip3", "SIG_A__SIG_P", 0, 45), row("trip3", "SIG_P__SIG_B", 45, 90, flags=1)]

    df = pd.DataFrame(rows)
    path = tmp_path / "service_date=2026-05-05"
    path.mkdir()
    pq.write_table(pa.Table.from_pandas(df), path / "route=22.parquet")

    registry = {"traversal_map": {"S1": [["SIG_A__SIG_P", "SIG_A__SIG_B"],
                                         ["SIG_P__SIG_B", "SIG_A__SIG_B"]]}}
    return str(tmp_path / "service_date=*" / "route=*.parquet"), registry


def test_view_merges_exactly_and_enforces_coverage(synthetic):
    glob, registry = synthetic
    con = duckdb.connect()
    create_canonical_view(con, glob, registry, get_city("cta"))
    got = con.execute(
        "SELECT trip_key, t_obs_s, flags, hour_local, period FROM trav ORDER BY trip_key"
    ).fetchall()
    by_trip = {r[0]: r for r in got}
    assert set(by_trip) == {"trip1", "trip3"}  # trip2 lacked coverage
    assert by_trip["trip1"][1] == pytest.approx(95.0)  # 40 + 55, exact
    assert by_trip["trip3"][1] == pytest.approx(90.0)
    assert by_trip["trip3"][2] & 1  # terminal flag propagated
    # 13:00 UTC == 08:00 Chicago -> am_peak (regression for the tz double-conversion bug)
    assert by_trip["trip1"][3] == 8
    assert by_trip["trip1"][4] == "am_peak"


def test_view_door_sidecar_merge(synthetic, tmp_path):
    """Door sidecar sums over constituents; uncovered trips report has_door=False."""
    glob, registry = synthetic
    side = tmp_path / "door_sidecar"
    side.mkdir()
    # trip1's vehicle covered: rows for BOTH constituents (12s + 20s dwell,
    # 3+2 ons); trip3's vehicle NOT covered (no rows at all).
    df = pd.DataFrame([
        {"trip_key": "trip1", "seg_id": "SIG_A__SIG_P", "shape_id": "S1",
         "door_n": 1, "dwell_s": 12.0, "ons": 3, "offs": 1, "load_sum": 20},
        {"trip_key": "trip1", "seg_id": "SIG_P__SIG_B", "shape_id": "S1",
         "door_n": 2, "dwell_s": 20.0, "ons": 2, "offs": 0, "load_sum": 41},
    ])
    pq.write_table(pa.Table.from_pandas(df), side / "service_date=2026-05-05.parquet")

    con = duckdb.connect()
    create_canonical_view(
        con, glob, registry, get_city("cta"),
        door_sidecar_glob=str(side / "service_date=*.parquet"),
    )
    got = {r[0]: r for r in con.execute(
        "SELECT trip_key, has_door, door_n, dwell_s, ons, offs, load_sum FROM trav"
    ).fetchall()}
    assert got["trip1"][1] is True
    assert got["trip1"][2] == 3          # 1 + 2 door cycles
    assert got["trip1"][3] == pytest.approx(32.0)
    assert got["trip1"][4] == 5 and got["trip1"][5] == 1
    assert got["trip1"][6] == 61
    assert got["trip3"][1] is False      # vehicle-day not covered
    assert got["trip3"][3] == pytest.approx(0.0)


def test_view_without_sidecar_defaults(synthetic):
    glob, registry = synthetic
    con = duckdb.connect()
    create_canonical_view(con, glob, registry, get_city("cta"))
    r = con.execute("SELECT has_door, door_n, dwell_s FROM trav LIMIT 1").fetchone()
    assert r[0] is False and r[1] == 0 and r[2] == 0.0
