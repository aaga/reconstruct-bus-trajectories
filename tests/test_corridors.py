"""Corridor chaining + direction pairing on synthetic registries."""

from __future__ import annotations

from analysis.network.corridors import chain_corridors, pair_directions


def _seg(seg_id, routes, *, len_m=500.0, rev=None, name="Main St", direction="North"):
    return {
        "seg_id": seg_id,
        "len_m": len_m,
        "name": name,
        "rev_seg_id": rev,
        "routes": [{"route_id": r, "direction": direction} for r in routes],
    }


def test_basic_chain_of_shared_segments():
    segments = {
        "A": _seg("A", ["r1", "r2"]),
        "B": _seg("B", ["r1", "r2"]),
        "C": _seg("C", ["r1", "r2"]),
        "D": _seg("D", ["r1"]),  # single-route: not shared
    }
    seqs = {"s1": ["A", "B", "C", "D"], "s2": ["A", "B", "C"]}
    chains = chain_corridors(segments, seqs, min_routes=2, min_length_m=1000.0)
    assert len(chains) == 1
    assert chains[0]["seg_ids"] == ["A", "B", "C"]
    assert chains[0]["routes"] == ["r1", "r2"]
    assert chains[0]["len_m"] == 1500.0


def test_chain_cut_when_route_intersection_drops():
    # A,B carried by r1+r2; C,D carried by r3+r4 — both shared, but the
    # running intersection empties at the B->C boundary, so two chains.
    segments = {
        "A": _seg("A", ["r1", "r2"]),
        "B": _seg("B", ["r1", "r2"]),
        "C": _seg("C", ["r3", "r4"]),
        "D": _seg("D", ["r3", "r4"]),
    }
    seqs = {
        "s1": ["A", "B", "C", "D"],  # some shape rides the whole street
        "s2": ["A", "B"],
        "s3": ["C", "D"],
    }
    chains = chain_corridors(segments, seqs, min_routes=2, min_length_m=0.0)
    ids = sorted(tuple(c["seg_ids"]) for c in chains)
    assert ids == [("A", "B"), ("C", "D")]


def test_min_length_drops_short_chains():
    segments = {
        "A": _seg("A", ["r1", "r2"], len_m=100.0),
        "B": _seg("B", ["r1", "r2"], len_m=100.0),
    }
    seqs = {"s1": ["A", "B"], "s2": ["A", "B"]}
    assert chain_corridors(segments, seqs, min_routes=2, min_length_m=800.0) == []
    assert len(chain_corridors(segments, seqs, min_routes=2, min_length_m=150.0)) == 1


def test_branch_follows_larger_intersection_and_restarts_other():
    # After B, three routes continue to C but only two to X.
    segments = {
        "A": _seg("A", ["r1", "r2", "r3"]),
        "B": _seg("B", ["r1", "r2", "r3"]),
        "C": _seg("C", ["r1", "r2", "r3"]),
        "X": _seg("X", ["r3", "r9"]),
        "Y": _seg("Y", ["r3", "r9"]),
    }
    seqs = {
        "s1": ["A", "B", "C"],
        "s2": ["A", "B", "C"],
        "s3": ["A", "B", "X", "Y"],
        "s4": ["X", "Y"],
    }
    chains = chain_corridors(segments, seqs, min_routes=2, min_length_m=0.0)
    by_first = {c["seg_ids"][0]: c for c in chains}
    assert by_first["A"]["seg_ids"] == ["A", "B", "C"]
    assert by_first["X"]["seg_ids"] == ["X", "Y"]


def test_pair_directions_merges_reverse_chains_even_when_split():
    # Northbound is one chain (A,B,C,D); southbound is split into two chains
    # (D',C') and (B',A') — component grouping must still fold all three
    # into one bidirectional corridor.
    def nseg(sid, rev):
        return _seg(sid, ["r1", "r2"], rev=rev, direction="North")

    def sseg(sid, rev):
        return _seg(sid, ["r1", "r2"], rev=rev, direction="South")

    segments = {
        "A": nseg("A", "Ar"), "B": nseg("B", "Br"),
        "C": nseg("C", "Cr"), "D": nseg("D", "Dr"),
        "Ar": sseg("Ar", "A"), "Br": sseg("Br", "B"),
        "Cr": sseg("Cr", "C"), "Dr": sseg("Dr", "D"),
    }
    chains = [
        {"seg_ids": ["A", "B", "C", "D"], "routes": ["r1", "r2"], "len_m": 2000.0},
        {"seg_ids": ["Dr", "Cr"], "routes": ["r1", "r2"], "len_m": 1000.0},
        {"seg_ids": ["Br", "Ar"], "routes": ["r1", "r2"], "len_m": 1000.0},
    ]
    corridors = pair_directions(chains, segments)
    assert len(corridors) == 1
    c = corridors[0]
    assert c["dir_fwd"] == "North"
    assert c["dir_rev"] == "South"
    assert c["seg_ids_fwd"] == ["A", "B", "C", "D"]
    assert set(c["seg_ids_rev"]) == {"Ar", "Br", "Cr", "Dr"}


def test_pair_directions_leaves_unpaired_chain_one_directional():
    segments = {
        "A": _seg("A", ["r1", "r2"], rev=None),
        "B": _seg("B", ["r1", "r2"], rev=None),
    }
    chains = [{"seg_ids": ["A", "B"], "routes": ["r1", "r2"], "len_m": 1000.0}]
    corridors = pair_directions(chains, segments)
    assert len(corridors) == 1
    assert corridors[0]["dir_rev"] is None
    assert corridors[0]["seg_ids_rev"] == []


def test_dominant_name_prefers_street_over_highway_ref():
    from analysis.network.corridors import _dominant_name

    segments = {
        "A": _seg("A", ["r1"], len_m=3000.0, name="US 41"),
        "B": _seg("B", ["r1"], len_m=400.0, name="South Lake Park Avenue"),
    }
    assert _dominant_name(segments, ["A", "B"]) == "South Lake Park Avenue"
    # But a ref wins when it's all there is.
    assert _dominant_name(segments, ["A"]) == "US 41"
