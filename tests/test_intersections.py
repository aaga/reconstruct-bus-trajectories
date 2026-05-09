"""Tests for intersection enrichment.

Synthetic OSM payloads + synthetic GTFS shapes — no Overpass / network
required. Each test builds a small `{elements: [...]}` Overpass-shaped dict,
plus a way-cache, plus a GTFS polyline, and verifies which intersections
get classified.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from bus_trajectories.intersections import (
    ControlPoint,
    build_intersections,
    cluster_signals,
    find_intersections_for_shape,
    load_intersections,
    save_intersections,
)
from bus_trajectories.way_match import WaySegment


# ---------- helpers --------------------------------------------------------


def _straight_polyline(n: int = 11, total_m: float = 1000.0, lat0: float = 41.9,
                       lon0: float = -87.65) -> tuple[np.ndarray, np.ndarray]:
    """East-west polyline of length total_m starting at (lat0, lon0)."""
    mlon = 111320.0 * math.cos(math.radians(lat0))
    spacing_lon = (total_m / (n - 1)) / mlon
    lons = lon0 + np.arange(n) * spacing_lon
    lats = np.full(n, lat0)
    poly = np.column_stack([lats, lons])
    dist = np.array([i * (total_m / (n - 1)) for i in range(n)])
    return poly, dist


def _osm(elements: list[dict]) -> dict:
    return {"elements": elements}


def _way(way_id: int, node_ids: list[int], tags: dict | None = None) -> dict:
    return {"type": "way", "id": way_id, "nodes": node_ids, "tags": tags or {}}


def _node(node_id: int, lat: float, lon: float, tags: dict | None = None) -> dict:
    return {"type": "node", "id": node_id, "lat": lat, "lon": lon, "tags": tags or {}}


# ---------- core tests -----------------------------------------------------


def test_signalized_4way_intersection():
    """Bus's way crosses a cross way; intersection node is tagged
    highway=traffic_signals → one ControlPoint of type traffic_signals."""
    poly, dist = _straight_polyline(n=11, total_m=1000.0)
    # Bus's way: nodes 1..11 (matching polyline vertex order).
    bus_node_ids = list(range(1, 12))
    bus_way = _way(100, bus_node_ids, tags={"highway": "primary",
                                              "name": "North Clark Street"})
    # Cross street's way: nodes 100, 6, 200 (intersecting at node 6, which
    # is the middle of the bus's polyline).
    cross_way = _way(200, [100, 6, 200], tags={"highway": "secondary",
                                                 "name": "West Belmont Avenue"})

    # Build node entries. Use the polyline lat/lons for the bus's nodes.
    elements: list[dict] = [bus_way, cross_way]
    for i, nid in enumerate(bus_node_ids):
        # Node 6 is the intersection — tag it traffic_signals.
        tags = {"highway": "traffic_signals"} if nid == 6 else None
        elements.append(_node(nid, float(poly[i, 0]), float(poly[i, 1]), tags))
    # Cross way's other nodes: just need to exist as members; lat/lon doesn't matter.
    elements.append(_node(100, float(poly[5, 0]) + 0.001, float(poly[5, 1])))
    elements.append(_node(200, float(poly[5, 0]) - 0.001, float(poly[5, 1])))

    cache = [WaySegment(way_id=100, dist_start_m=0.0, dist_end_m=1000.0,
                         direction="forward", name="North Clark Street",
                         road_class="primary")]
    cps = find_intersections_for_shape(cache, poly, dist, _osm(elements))
    assert len(cps) == 1
    assert cps[0].control_type == "traffic_signals"
    assert cps[0].intersection_node_id == 6
    assert cps[0].on_way_id == 100
    assert "West Belmont Avenue" in cps[0].cross_street_names
    assert abs(cps[0].dist_along_route_m - 500.0) < 1.0


def test_stop_sign_on_bus_approach():
    """A node 20 m before the intersection on the bus's way carries
    highway=stop with direction=forward; bus traverses forward → stop event."""
    # 51 vertices over 1000m → 20m spacing; stop at vertex 9 (dist 180m) and
    # intersection at vertex 10 (dist 200m), 20m apart (within 30m threshold).
    poly, dist = _straight_polyline(n=51, total_m=1000.0)
    bus_node_ids = list(range(1, 52))
    bus_way = _way(100, bus_node_ids, tags={"highway": "primary",
                                              "name": "Bus Way"})
    cross_way = _way(200, [100, 10, 200], tags={"highway": "residential",
                                                  "name": "Side St"})
    elements: list[dict] = [bus_way, cross_way]
    for i, nid in enumerate(bus_node_ids):
        tags = None
        if nid == 9:
            tags = {"highway": "stop", "direction": "forward"}
        elements.append(_node(nid, float(poly[i, 0]), float(poly[i, 1]), tags))
    elements.append(_node(100, float(poly[9, 0]) + 0.001, float(poly[9, 1])))
    elements.append(_node(200, float(poly[9, 0]) - 0.001, float(poly[9, 1])))

    cache = [WaySegment(way_id=100, dist_start_m=0.0, dist_end_m=1000.0,
                         direction="forward", name="Bus Way",
                         road_class="primary")]
    cps = find_intersections_for_shape(cache, poly, dist, _osm(elements))
    assert len(cps) == 1
    assert cps[0].control_type == "stop"
    assert cps[0].intersection_node_id == 10
    assert "Side St" in cps[0].cross_street_names


def test_stop_sign_on_cross_street_only_skipped():
    """Cross street has stop nodes; bus's way has no stop → 0 ControlPoints
    (bus has free right-of-way)."""
    poly, dist = _straight_polyline(n=11, total_m=1000.0)
    bus_node_ids = list(range(1, 12))
    # Cross way's controlled-approach node (the stop sign) is on the cross
    # way's node list, NOT on the bus's way.
    bus_way = _way(100, bus_node_ids, tags={"highway": "primary"})
    cross_way = _way(200, [100, 99, 6, 199, 200],
                     tags={"highway": "residential", "name": "Side St"})
    elements: list[dict] = [bus_way, cross_way]
    for i, nid in enumerate(bus_node_ids):
        elements.append(_node(nid, float(poly[i, 0]), float(poly[i, 1])))
    # Cross way nodes; node 99 is the stop sign on the cross street's approach.
    elements.append(_node(100, float(poly[5, 0]) + 0.002, float(poly[5, 1])))
    elements.append(_node(99,  float(poly[5, 0]) + 0.0001, float(poly[5, 1]),
                          tags={"highway": "stop", "direction": "forward"}))
    elements.append(_node(199, float(poly[5, 0]) - 0.0001, float(poly[5, 1])))
    elements.append(_node(200, float(poly[5, 0]) - 0.002, float(poly[5, 1])))

    cache = [WaySegment(way_id=100, dist_start_m=0.0, dist_end_m=1000.0,
                         direction="forward", name="Bus", road_class="primary")]
    cps = find_intersections_for_shape(cache, poly, dist, _osm(elements))
    # The intersection node 6 has no traffic_signals tag and no stop on the
    # bus's way → uncontrolled → no event.
    assert cps == []


def test_direction_mismatch_skipped():
    """Stop on bus's way with direction=backward; bus traverses forward →
    stop doesn't apply."""
    poly, dist = _straight_polyline(n=51, total_m=1000.0)
    bus_node_ids = list(range(1, 52))
    bus_way = _way(100, bus_node_ids, tags={"highway": "primary"})
    cross_way = _way(200, [100, 10, 200], tags={"highway": "residential"})
    elements: list[dict] = [bus_way, cross_way]
    for i, nid in enumerate(bus_node_ids):
        tags = None
        if nid == 9:
            tags = {"highway": "stop", "direction": "backward"}  # mismatched
        elements.append(_node(nid, float(poly[i, 0]), float(poly[i, 1]), tags))
    elements.append(_node(100, float(poly[9, 0]) + 0.001, float(poly[9, 1])))
    elements.append(_node(200, float(poly[9, 0]) - 0.001, float(poly[9, 1])))

    cache = [WaySegment(way_id=100, dist_start_m=0.0, dist_end_m=1000.0,
                         direction="forward", name="Bus", road_class="primary")]
    cps = find_intersections_for_shape(cache, poly, dist, _osm(elements))
    assert cps == []


def test_dedupe_by_intersection_node():
    """Same intersection node touched by two segments (e.g., bus's way is
    split into two cache entries that meet at the intersection) → one
    ControlPoint, not two."""
    poly, dist = _straight_polyline(n=11, total_m=1000.0)
    bus_node_ids_a = list(range(1, 7))     # 1..6
    bus_node_ids_b = list(range(6, 12))    # 6..11 — shares node 6 with way A
    way_a = _way(100, bus_node_ids_a, tags={"highway": "primary"})
    way_b = _way(101, bus_node_ids_b, tags={"highway": "primary"})
    cross_way = _way(200, [100, 6, 200], tags={"highway": "secondary",
                                                 "name": "Cross St"})
    elements: list[dict] = [way_a, way_b, cross_way]
    for i, nid in enumerate(range(1, 12)):
        tags = {"highway": "traffic_signals"} if nid == 6 else None
        elements.append(_node(nid, float(poly[i, 0]), float(poly[i, 1]), tags))
    elements.append(_node(100, float(poly[5, 0]) + 0.001, float(poly[5, 1])))
    elements.append(_node(200, float(poly[5, 0]) - 0.001, float(poly[5, 1])))

    cache = [
        WaySegment(way_id=100, dist_start_m=0.0, dist_end_m=500.0,
                   direction="forward", name="Bus", road_class="primary"),
        WaySegment(way_id=101, dist_start_m=500.0, dist_end_m=1000.0,
                   direction="forward", name="Bus", road_class="primary"),
    ]
    cps = find_intersections_for_shape(cache, poly, dist, _osm(elements))
    # Only one ControlPoint despite the intersection node being on both ways.
    assert len(cps) == 1
    assert cps[0].intersection_node_id == 6


def test_off_route_node_skipped():
    """A node 100 m off the GTFS polyline is not classified as on-route,
    even if it's nominally in one of the ways."""
    poly, dist = _straight_polyline(n=11, total_m=1000.0)
    bus_node_ids = list(range(1, 12))
    bus_way = _way(100, bus_node_ids, tags={"highway": "primary"})
    cross_way = _way(200, [100, 99, 200], tags={"highway": "residential"})
    elements: list[dict] = [bus_way, cross_way]
    for i, nid in enumerate(bus_node_ids):
        elements.append(_node(nid, float(poly[i, 0]), float(poly[i, 1])))
    # Node 99 is "highway=traffic_signals" but located 100m off the polyline.
    elements.append(_node(99, float(poly[5, 0]) + (100.0 / 111320.0),
                          float(poly[5, 1]),
                          tags={"highway": "traffic_signals"}))
    elements.append(_node(100, float(poly[5, 0]) + 0.002, float(poly[5, 1])))
    elements.append(_node(200, float(poly[5, 0]) - 0.002, float(poly[5, 1])))

    cache = [WaySegment(way_id=100, dist_start_m=0.0, dist_end_m=1000.0,
                         direction="forward", name="Bus", road_class="primary")]
    cps = find_intersections_for_shape(cache, poly, dist, _osm(elements))
    # The off-route signal node isn't on the bus's polyline (perp > 30m), and
    # no other intersection exists here, so no ControlPoints.
    assert cps == []


# ---------- direction-applies edge cases ----------------------------------


def test_stop_with_direction_absent_applies_both_ways():
    """A stop sign with no direction tag applies to both forward and reverse
    traversal (conservative)."""
    poly, dist = _straight_polyline(n=51, total_m=1000.0)
    bus_node_ids = list(range(1, 52))
    bus_way = _way(100, bus_node_ids, tags={"highway": "primary"})
    cross_way = _way(200, [100, 10, 200], tags={"highway": "residential"})
    elements: list[dict] = [bus_way, cross_way]
    for i, nid in enumerate(bus_node_ids):
        tags = {"highway": "stop"} if nid == 9 else None  # no direction tag
        elements.append(_node(nid, float(poly[i, 0]), float(poly[i, 1]), tags))
    elements.append(_node(100, float(poly[9, 0]) + 0.001, float(poly[9, 1])))
    elements.append(_node(200, float(poly[9, 0]) - 0.001, float(poly[9, 1])))

    cache_fwd = [WaySegment(way_id=100, dist_start_m=0.0, dist_end_m=1000.0,
                             direction="forward", name="Bus", road_class="primary")]
    cps = find_intersections_for_shape(cache_fwd, poly, dist, _osm(elements))
    assert len(cps) == 1 and cps[0].control_type == "stop"


# ---------- clustering ----------------------------------------------------


def _cp(node_id: int, dist_m: float, control_type: str = "traffic_signals",
        cross: tuple[str, ...] = ()) -> ControlPoint:
    return ControlPoint(
        intersection_node_id=node_id,
        lat=41.9, lon=-87.65,
        dist_along_route_m=dist_m,
        on_way_id=100,
        control_type=control_type,
        cross_street_names=cross,
    )


def test_cluster_merges_close():
    """Two signal nodes within max_gap_m → merged regardless of cross-street."""
    cps = [
        _cp(1, 1000.0, cross=("West Belmont Avenue",)),
        _cp(2, 1100.0, cross=("North Halsted Street",)),
    ]
    out = cluster_signals(cps, max_gap_m=200)
    assert len(out) == 1
    assert out[0].intersection_node_id == 1
    assert 2 in out[0].merged_node_ids
    # Union of cross-street names.
    assert set(out[0].cross_street_names) == {
        "West Belmont Avenue", "North Halsted Street",
    }


def test_cluster_keeps_distant_separate():
    """Two signals farther than max_gap_m → not merged."""
    cps = [
        _cp(1, 1000.0, cross=("West Belmont Avenue",)),
        _cp(2, 1480.0, cross=("West Belmont Avenue",)),
    ]
    out = cluster_signals(cps, max_gap_m=200)
    assert len(out) == 2
    assert all(c.merged_node_ids == () for c in out)


def test_cluster_chains_three_into_one():
    """Three signals each within max_gap_m of the next → chained into one."""
    cps = [
        _cp(1, 1000.0, cross=("A",)),
        _cp(2, 1100.0, cross=("B",)),
        _cp(3, 1190.0, cross=("C",)),
    ]
    out = cluster_signals(cps, max_gap_m=200)
    assert len(out) == 1
    assert sorted(out[0].merged_node_ids) == [2, 3]
    assert set(out[0].cross_street_names) == {"A", "B", "C"}


def test_cluster_does_not_merge_across_types():
    """A signal and a stop sign within distance threshold → kept separate
    (they're not the same kind of control)."""
    cps = [
        _cp(1, 1000.0, control_type="traffic_signals", cross=("West Foster Avenue",)),
        _cp(2, 1080.0, control_type="stop", cross=("West Foster Avenue",)),
    ]
    out = cluster_signals(cps, max_gap_m=200)
    assert len(out) == 2


def test_give_way_filtered_by_default():
    """A give_way entry should be dropped from the output by default."""
    poly, dist = _straight_polyline(n=51, total_m=1000.0)
    bus_node_ids = list(range(1, 52))
    bus_way = _way(100, bus_node_ids, tags={"highway": "primary"})
    cross_way = _way(200, [100, 10, 200], tags={"highway": "residential"})
    elements: list[dict] = [bus_way, cross_way]
    for i, nid in enumerate(bus_node_ids):
        tags = {"highway": "give_way"} if nid == 9 else None
        elements.append(_node(nid, float(poly[i, 0]), float(poly[i, 1]), tags))
    elements.append(_node(100, float(poly[9, 0]) + 0.001, float(poly[9, 1])))
    elements.append(_node(200, float(poly[9, 0]) - 0.001, float(poly[9, 1])))
    cache = [WaySegment(way_id=100, dist_start_m=0.0, dist_end_m=1000.0,
                         direction="forward", name="Bus", road_class="primary")]
    # Default keep_types excludes give_way.
    cps = find_intersections_for_shape(cache, poly, dist, _osm(elements))
    assert cps == []
    # But include give_way explicitly → kept.
    cps2 = find_intersections_for_shape(cache, poly, dist, _osm(elements),
                                          keep_types=("traffic_signals", "stop", "give_way"))
    assert len(cps2) == 1 and cps2[0].control_type == "give_way"


# ---------- save/load round-trip -------------------------------------------


def test_save_load_roundtrip(tmp_path: Path):
    data = {
        "shape_a": [
            ControlPoint(
                intersection_node_id=12345,
                lat=41.93,
                lon=-87.65,
                dist_along_route_m=1234.5,
                on_way_id=100,
                control_type="traffic_signals",
                cross_street_names=("West Belmont Avenue",),
            ),
            ControlPoint(
                intersection_node_id=67890,
                lat=41.94,
                lon=-87.66,
                dist_along_route_m=4567.0,
                on_way_id=101,
                control_type="stop",
                cross_street_names=(),
            ),
        ],
        "shape_b": [],
    }
    p = tmp_path / "intersections.json"
    save_intersections(data, p)
    loaded = load_intersections(p)
    assert loaded == data
