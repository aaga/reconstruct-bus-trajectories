"""Tests for the simplified way-match module.

All synthetic Valhalla payloads — no network or Valhalla running required.
The new algorithm walks Valhalla's encoded ``shape`` polyline, projects each
shape vertex onto the GTFS polyline, and uses ``begin/end_shape_index`` to
slice each edge's bounds. We synthesise both the shape and the edge-index
ranges in these tests.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest

from dataio.way_match import (
    WaySegment,
    decode_polyline6,
    extract_segments,
    load_cache,
    save_cache,
)


# ---------- helpers --------------------------------------------------------


def encode_polyline6(coords: list[tuple[float, float]]) -> str:
    """Encode (lat, lon) list to polyline6 (round-trip with decode_polyline6)."""
    s = []
    plat = plon = 0
    for lat, lon in coords:
        ilat = round(lat * 1e6)
        ilon = round(lon * 1e6)
        for d in (ilat - plat, ilon - plon):
            d = (d << 1) ^ (d >> 31)
            while d >= 0x20:
                s.append(chr((0x20 | (d & 0x1f)) + 63))
                d >>= 5
            s.append(chr(d + 63))
        plat, plon = ilat, ilon
    return "".join(s)


def _make_response(*, edges: list[dict], shape_coords: list[tuple[float, float]]) -> dict:
    return {
        "edges": edges,
        "matched_points": [],
        "shape": encode_polyline6(shape_coords),
    }


def _straight_polyline(n: int = 11, total_m: float = 1000.0) -> tuple[np.ndarray, np.ndarray]:
    """East-west straight polyline of `total_m` length, `n` evenly spaced vertices."""
    lat0 = 41.9
    mlon = 111320.0 * math.cos(math.radians(lat0))
    spacing_lon = (total_m / (n - 1)) / mlon
    lons = -87.65 + np.arange(n) * spacing_lon
    lats = np.full(n, lat0)
    poly = np.column_stack([lats, lons])
    dist = np.array([i * (total_m / (n - 1)) for i in range(n)])
    return poly, dist


# ---------- decode round-trip ---------------------------------------------


def test_decode_polyline6_roundtrip():
    coords = [(41.93, -87.65), (41.94, -87.66), (42.00, -87.67)]
    encoded = encode_polyline6(coords)
    decoded = decode_polyline6(encoded)
    for a, b in zip(coords, decoded):
        assert abs(a[0] - b[0]) < 1e-5
        assert abs(a[1] - b[1]) < 1e-5


# ---------- extract_segments ----------------------------------------------


def test_extract_segments_two_edges_chain_perfectly():
    """Two edges that share a shape index → resulting segments touch with no gap."""
    poly, dist = _straight_polyline(n=11, total_m=1000.0)
    # Matched shape: 3 vertices at 0m, 500m, 1000m along the GTFS polyline.
    # Pick the same lat/lon as the corresponding GTFS vertices.
    shape = [(poly[0, 0], poly[0, 1]), (poly[5, 0], poly[5, 1]), (poly[10, 0], poly[10, 1])]
    edges = [
        {"way_id": 100, "names": ["A St"], "forward": True, "road_class": "primary",
         "begin_shape_index": 0, "end_shape_index": 1},
        {"way_id": 200, "names": ["B St"], "forward": False, "road_class": "secondary",
         "begin_shape_index": 1, "end_shape_index": 2},
    ]
    segs = extract_segments(_make_response(edges=edges, shape_coords=shape), poly, dist)
    assert len(segs) == 2
    assert segs[0].way_id == 100
    assert segs[1].way_id == 200
    # Edges share shape index 1 — boundaries match.
    assert abs(segs[0].dist_end_m - segs[1].dist_start_m) < 1e-3
    # No gap, no overlap.
    assert segs[0].dist_start_m < segs[0].dist_end_m
    assert segs[1].dist_start_m < segs[1].dist_end_m
    # Direction propagated.
    assert segs[0].direction == "forward"
    assert segs[1].direction == "reverse"
    # First & last clamped to bounds.
    assert segs[0].dist_start_m == 0.0
    assert abs(segs[-1].dist_end_m - 1000.0) < 1.0


def test_extract_segments_clamps_first_and_last():
    """source_percent_along trims the route start; we clamp to 0."""
    poly, dist = _straight_polyline(n=11, total_m=1000.0)
    # Matched shape STARTS at GTFS vertex 1 (= 100m along) — Valhalla trimmed.
    shape = [(poly[1, 0], poly[1, 1]), (poly[10, 0], poly[10, 1])]
    edges = [
        {"way_id": 7, "forward": True,
         "begin_shape_index": 0, "end_shape_index": 1,
         "source_percent_along": 0.1},
    ]
    segs = extract_segments(_make_response(edges=edges, shape_coords=shape), poly, dist)
    assert len(segs) == 1
    # Clamp pulls first segment's dist_start to 0 (was 100m).
    assert segs[0].dist_start_m == 0.0
    # And last to shape_length.
    assert abs(segs[0].dist_end_m - 1000.0) < 1.0


def test_extract_segments_full_coverage_no_gaps():
    """A multi-edge route should tile the GTFS shape with zero gap and zero overlap."""
    poly, dist = _straight_polyline(n=21, total_m=2000.0)
    shape = [(poly[i, 0], poly[i, 1]) for i in [0, 5, 10, 15, 20]]
    edges = [
        {"way_id": 1, "forward": True, "begin_shape_index": 0, "end_shape_index": 1},
        {"way_id": 2, "forward": True, "begin_shape_index": 1, "end_shape_index": 2},
        {"way_id": 3, "forward": True, "begin_shape_index": 2, "end_shape_index": 3},
        {"way_id": 4, "forward": True, "begin_shape_index": 3, "end_shape_index": 4},
    ]
    segs = extract_segments(_make_response(edges=edges, shape_coords=shape), poly, dist)
    assert len(segs) == 4
    # Adjacent segments touch
    for a, b in zip(segs, segs[1:]):
        assert abs(b.dist_start_m - a.dist_end_m) < 1e-3
    # First at 0, last at shape_length
    assert segs[0].dist_start_m == 0.0
    assert abs(segs[-1].dist_end_m - 2000.0) < 1.0
    # Total covered ≈ shape length
    covered = sum(s.dist_end_m - s.dist_start_m for s in segs)
    assert abs(covered - 2000.0) < 1.0


def test_extract_segments_repeated_way_id():
    """Same way appearing twice (different edges) → two distinct segments."""
    poly, dist = _straight_polyline(n=21, total_m=2000.0)
    shape = [(poly[i, 0], poly[i, 1]) for i in [0, 5, 10, 20]]
    edges = [
        {"way_id": 42, "forward": True, "begin_shape_index": 0, "end_shape_index": 1},
        {"way_id": 99, "forward": True, "begin_shape_index": 1, "end_shape_index": 2},
        {"way_id": 42, "forward": False, "begin_shape_index": 2, "end_shape_index": 3},
    ]
    segs = extract_segments(_make_response(edges=edges, shape_coords=shape), poly, dist)
    assert [s.way_id for s in segs] == [42, 99, 42]
    assert segs[0].direction == "forward"
    assert segs[2].direction == "reverse"


def test_extract_segments_no_shape_returns_empty():
    poly, dist = _straight_polyline()
    assert extract_segments({"edges": [{"way_id": 1, "begin_shape_index": 0, "end_shape_index": 1}],
                             "shape": ""}, poly, dist) == []
    assert extract_segments({}, poly, dist) == []


def test_extract_segments_skips_edges_missing_indices():
    poly, dist = _straight_polyline()
    shape = [(poly[0, 0], poly[0, 1]), (poly[10, 0], poly[10, 1])]
    edges = [
        {"way_id": 1, "begin_shape_index": 0, "end_shape_index": 1, "forward": True},
        {"way_id": 2, "forward": True},  # no shape indices
    ]
    segs = extract_segments(_make_response(edges=edges, shape_coords=shape), poly, dist)
    assert len(segs) == 1
    assert segs[0].way_id == 1


def test_save_load_roundtrip(tmp_path: Path):
    cache = {
        "shape_alpha": [
            WaySegment(way_id=42, dist_start_m=0.0, dist_end_m=100.0,
                       direction="forward", name="Main St", road_class="primary"),
            WaySegment(way_id=99, dist_start_m=100.0, dist_end_m=200.0,
                       direction="reverse", name=None, road_class=None),
        ],
        "shape_failed": [],
    }
    p = tmp_path / "cache.json"
    save_cache(cache, p)
    loaded = load_cache(p)
    assert loaded == cache
