"""Build the canonical cross-route segment registry for a city.

Segmentation for the network analysis differs from the chapter-3 corridor
study in two deliberate ways (2026-07 decisions):

1. **Boundaries are traffic signals only.** Mid-block ped signals stay in the
   intersections cache as control points (visible to future delay
   attribution) but no longer split segments ("demote, don't delete").
2. **Global 30 m node clustering.** The per-shape Overpass pipeline assigns
   different OSM node ids to the same physical intersection for different
   shapes (dual-carriageway nodes, stacked signal nodes). All boundary-signal
   nodes are clustered network-wide (union-find, 30 m = the corridor study's
   DEFAULT_CLUSTER_GAP_M); each cluster's canonical id is the node used by
   the most shapes (tie: lowest id). Segments whose endpoints fall in the
   same cluster ("confetti" inside one big intersection) are dropped.

Because the 86-day traversal batch was computed against the LEGACY
segmentation, the registry also emits ``traversal_map``: per shape, each old
seg_id → its canonical seg_id. Old constituents tile canonical spans exactly
(t_obs is additive at shared boundaries), so downstream consumers merge
per-trip without recomputing reconstructions — see ``traversals_view.py``.

Usage:
    PYTHONPATH=src uv run python analysis/network/registry.py --city cta
Output:
    outputs/network/<city>/segment_registry.json
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io as _io
import json
import sys
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from analysis.prep.geometry import (  # noqa: E402
    simplify_polyline,
    slice_polyline,
)
from core.decompose.segments import Segment, build_segments_from_records  # noqa: E402
from dataio.cities import CityConfig, get_city  # noqa: E402
from dataio.gtfs import list_bus_shapes, load_gtfs_shape_with_dist  # noqa: E402
from dataio.exceptions import load_exceptions  # noqa: E402
from dataio.intersections import load_intersections  # noqa: E402

LEN_OUTLIER_FRAC = 0.10  # instance length deviating >10% from median → flagged
BOUNDARY_TYPES = ("traffic_signals",)  # network decision: ped signals demoted
CLUSTER_RADIUS_M = 30.0  # matches intersections.DEFAULT_CLUSTER_GAP_M

# Door-peak stop location (2026-08 decision): a stop's position is its modal
# 10 ft bucket of door-event AVL fixes when trustworthy, else the GTFS pole.
DOOR_PEAK_BUCKET_M = 3.048
DOOR_PEAK_MIN_EVENTS = 5
DOOR_PEAK_MAX_POLE_DIST_M = 100.0  # beyond this the attribution is suspect

# GTFS `direction_id` fallback labels for trips whose human `direction`
# column is unpopulated (CTA leaves it "0" on a few thousand trips).
_DIRECTION_ID_LABEL = {"0": "dir0", "1": "dir1"}


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------
# Global boundary-node clustering (pure; unit-tested)
# --------------------------------------------------------------------------

def cluster_nodes(
    positions: dict[int, tuple[float, float]],  # node -> (lat, lon)
    usage: dict[int, int],  # node -> #shapes using it (for representative pick)
    radius_m: float = CLUSTER_RADIUS_M,
    primary: set[int] | frozenset[int] = frozenset(),
    extra_edges: list[tuple[int, int]] | tuple = (),
    split_sides: dict[int, tuple[int, int]] | None = None,
) -> dict[int, int]:
    """Union-find nodes within ``radius_m``; return node -> canonical node.

    Canonical = prefer a ``primary`` member (true highway=traffic_signals
    node), then the member used by the most shapes, then lowest node id —
    a deterministic global rule, unlike the corridor study's per-shape
    "first in route order".

    ``extra_edges`` unions specific node pairs regardless of distance —
    used to fold per-approach stop-line signals (which can sit > radius
    apart across a big junction) into one cluster via their shared anchor
    junction vertex. Pairs with ids missing from ``positions`` are ignored.

    ``split_sides`` (node -> (divide, side), from cluster_split exceptions)
    forbids any union that would put nodes from both sides of a divide in
    one component — including transitively through third-party nodes.
    """
    ids = sorted(positions)
    if not ids:
        return {}
    lat0 = float(np.mean([positions[n][0] for n in ids]))
    mlat = 111320.0 * np.cos(np.radians(lat0))
    xy = np.array([[positions[n][1] * mlat, positions[n][0] * 111320.0] for n in ids])

    cell: dict[tuple[int, int], list[int]] = defaultdict(list)
    for i, p in enumerate(xy):
        cell[(int(p[0] // radius_m), int(p[1] // radius_m))].append(i)

    parent = list(range(len(ids)))
    # component root -> {divide: side} constraint labels
    comp_sides: dict[int, dict[int, int]] = {}
    if split_sides:
        for i, n in enumerate(ids):
            s = split_sides.get(n)
            if s is not None:
                comp_sides[i] = {s[0]: s[1]}

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri == rj:
            return
        a, b = comp_sides.get(ri, {}), comp_sides.get(rj, {})
        for divide, side in b.items():
            if divide in a and a[divide] != side:
                return  # would bridge a cluster_split divide — refuse
        parent[rj] = ri
        if a or b:
            comp_sides[ri] = {**a, **b}
            comp_sides.pop(rj, None)

    for (cx, cy), members in cell.items():
        cand: list[int] = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                cand.extend(cell.get((cx + dx, cy + dy), []))
        for i in members:
            for j in cand:
                if i < j and np.hypot(*(xy[i] - xy[j])) < radius_m:
                    union(i, j)

    idx = {n: i for i, n in enumerate(ids)}
    for a, b in extra_edges:
        ia, ib = idx.get(a), idx.get(b)
        if ia is not None and ib is not None:
            union(ia, ib)

    groups: dict[int, list[int]] = defaultdict(list)
    for i, n in enumerate(ids):
        groups[find(i)].append(n)
    out: dict[int, int] = {}
    for members in groups.values():
        rep = max(members, key=lambda n: (n in primary, usage.get(n, 0), -n))
        for n in members:
            out[n] = rep
    return out


# --------------------------------------------------------------------------
# Batched GTFS readers (one pass per file)
# --------------------------------------------------------------------------

def shape_route_direction(gtfs_zip: Path) -> dict[str, dict]:
    """One pass over trips.txt: shape_id -> {route_id, direction, rep_trip_id}.

    Direction label priority: trips.txt ``direction`` column (CTA) →
    directions.txt (route_id, direction_id) names (MBTA "Outbound"/
    "Inbound") → generic dir0/dir1.
    """
    # Optional directions.txt: (route_id, direction_id) -> human name.
    dir_names: dict[tuple[str, str], str] = {}
    with zipfile.ZipFile(gtfs_zip) as z:
        if "directions.txt" in z.namelist():
            with z.open("directions.txt") as f:
                for r in csv.DictReader(_io.TextIOWrapper(f, encoding="utf-8-sig")):
                    name = (r.get("direction") or "").strip()
                    if name:
                        dir_names[(r["route_id"], r["direction_id"])] = name

    votes: dict[str, Counter] = defaultdict(Counter)
    route_of: dict[str, str] = {}
    rep_trip: dict[str, str] = {}
    with zipfile.ZipFile(gtfs_zip) as z, z.open("trips.txt") as f:
        for t in csv.DictReader(_io.TextIOWrapper(f, encoding="utf-8-sig")):
            sid = t.get("shape_id") or ""
            if not sid:
                continue
            label = (t.get("direction") or "").strip()
            if label in ("", "0", "1"):
                label = dir_names.get(
                    (t["route_id"], t.get("direction_id", "")),
                    _DIRECTION_ID_LABEL.get(t.get("direction_id", ""), "unknown"),
                )
            votes[sid][label] += 1
            route_of.setdefault(sid, t["route_id"])
            rep_trip.setdefault(sid, t["trip_id"])
    return {
        sid: {
            "route_id": route_of[sid],
            "direction": votes[sid].most_common(1)[0][0],
            "rep_trip_id": rep_trip[sid],
        }
        for sid in votes
    }


def _door_peaks(city) -> dict[str, tuple[float, float]]:
    """stop_id -> modal-10ft-bucket door-event position, for cities with a
    door cache. Empty dict when the cache is absent."""
    door_dir = city.resolve("caches/door_events") / city.city_id
    if not door_dir.exists() or not any(door_dir.glob("*.parquet")):
        return {}
    import math

    import duckdb

    mlon = 111320.0 * math.cos(math.radians(41.85 if city.city_id.startswith("cta")
                                            else 42.36))
    con = duckdb.connect()
    rows = con.execute(f"""
        WITH ev AS (
          SELECT stop_id, latitude, longitude,
                 floor(longitude * {mlon} / {DOOR_PEAK_BUCKET_M})::BIGINT AS bxx,
                 floor(latitude * 111320.0 / {DOOR_PEAK_BUCKET_M})::BIGINT AS byy
          FROM '{door_dir}/*.parquet'
          WHERE latitude IS NOT NULL AND stop_id IS NOT NULL
        ),
        b AS (
          SELECT stop_id, bxx, byy, count(*) AS n,
                 avg(latitude) AS lat, avg(longitude) AS lon
          FROM ev GROUP BY stop_id, bxx, byy
        ),
        rk AS (
          SELECT *, row_number() OVER (PARTITION BY stop_id
                                       ORDER BY n DESC, bxx, byy) AS rn,
                 sum(n) OVER (PARTITION BY stop_id) AS tot
          FROM b
        )
        SELECT stop_id, lat, lon FROM rk
        WHERE rn = 1 AND tot >= {DOOR_PEAK_MIN_EVENTS}
    """).fetchall()
    return {str(r[0]): (float(r[1]), float(r[2])) for r in rows}


def _dist_ll_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    import math

    mlat = 111320.0 * math.cos(math.radians((lat1 + lat2) / 2))
    return math.hypot((lon1 - lon2) * mlat, (lat1 - lat2) * 111320.0)


def stops_by_shape(gtfs_zip: Path, rep_trip_by_shape: dict[str, str],
                   city=None) -> dict[str, list[dict]]:
    """One pass over stop_times.txt + stops.txt: shape_id -> ordered stops.

    Stop distance-along is ALWAYS a physical position projected onto the
    shape polyline (2026-07-30 decision; stop_times.shape_dist_traveled is
    deliberately ignored — CTA generates it on the same glitched ruler as
    shapes.txt). The position, per stop (2026-08 decision, when ``city`` is
    given):

      1. ``stop_coord_override`` exception, if present;
      2. the stop's modal-10ft-bucket door-event peak, when the city has a
         door cache, the stop has >= DOOR_PEAK_MIN_EVENTS located events,
         the peak lies within DOOR_PEAK_MAX_POLE_DIST_M of the GTFS pole,
         and the stop is not door_peak_reject-ed — measured service
         location beats the agency's geocode;
      3. the GTFS pole (always, for cities without door data).

    ``terminal_stop`` exceptions are excluded entirely — off-street bays
    are outside the signal-to-signal segment model.
    """
    trip_to_shape = {v: k for k, v in rep_trip_by_shape.items()}
    rows_by_trip: dict[str, list[dict]] = defaultdict(list)
    with zipfile.ZipFile(gtfs_zip) as z:
        with z.open("stop_times.txt") as f:
            for r in csv.DictReader(_io.TextIOWrapper(f, encoding="utf-8-sig")):
                if r["trip_id"] in trip_to_shape:
                    rows_by_trip[r["trip_id"]].append(r)
        with z.open("stops.txt") as f:
            stops_meta = {
                r["stop_id"]: r
                for r in csv.DictReader(_io.TextIOWrapper(f, encoding="utf-8-sig"))
            }

    exc = load_exceptions(city.city_id) if city is not None else None
    peaks = _door_peaks(city) if city is not None else {}
    loc: dict[str, tuple[float, float]] = {}
    n_peak = n_override = 0
    for sid_, m in stops_meta.items():
        try:
            pole = (float(m.get("stop_lat") or "nan"),
                    float(m.get("stop_lon") or "nan"))
        except ValueError:
            continue
        if exc and sid_ in exc.coord_overrides:
            loc[sid_] = exc.coord_overrides[sid_]
            n_override += 1
            continue
        pk = peaks.get(sid_)
        if (pk is not None and not (exc and sid_ in exc.peak_rejects)
                and pole == pole  # not NaN
                and _dist_ll_m(*pk, *pole) <= DOOR_PEAK_MAX_POLE_DIST_M):
            loc[sid_] = pk
            n_peak += 1
        else:
            loc[sid_] = pole
    if n_peak or n_override:
        print(f"stops_by_shape: {n_peak} stops located by door peak, "
              f"{n_override} by coord override")

    from core.mapmatch.shape_snap import SnapToShapeMatcher

    out: dict[str, list[dict]] = {}
    n_dropped = n_terminal = 0
    for trip_id, rows in rows_by_trip.items():
        rows.sort(key=lambda r: int(r["stop_sequence"]))
        if exc is not None:
            kept = [r for r in rows if r["stop_id"] not in exc.terminal_stops]
            n_terminal += len(rows) - len(kept)
            rows = kept
        shape_id = trip_to_shape[trip_id]
        polyline, dist_m = load_gtfs_shape_with_dist(gtfs_zip, shape_id)
        matcher = SnapToShapeMatcher(
            polyline, max_perp_m=100.0, dist_along_m_per_vertex=dist_m
        )
        locs = [loc.get(r["stop_id"], (float("nan"), float("nan")))
                for r in rows]
        lats = np.array([p[0] for p in locs])
        lons = np.array([p[1] for p in locs])
        ok = ~(np.isnan(lats) | np.isnan(lons))
        res = matcher.match(np.where(ok, lats, 0.0), np.where(ok, lons, 0.0))
        stops = []
        for i, r in enumerate(rows):
            # A stop >100 m off its own shape is a feed inconsistency;
            # drop rather than pin a bogus distance to it.
            if not ok[i] or not res.on_route[i]:
                n_dropped += 1
                continue
            sid = r["stop_id"]
            stops.append(
                {
                    "stop_id": sid,
                    "name": stops_meta.get(sid, {}).get("stop_name", sid),
                    "dist_along_m": float(res.dist_along_m[i]),
                }
            )
        out[shape_id] = stops
    if n_dropped:
        print(f"stops_by_shape: dropped {n_dropped} stops >100 m off their shape")
    if n_terminal:
        print(f"stops_by_shape: excluded {n_terminal} terminal-stop rows "
              f"(exceptions)")
    return out


# --------------------------------------------------------------------------
# Geometry from OSM way chains (preferred over GTFS shape slices: follows the
# actual roadway; direction pairs on single-carriageway streets share the
# same ways and so overlap exactly, trimmed at canonical node positions)
# --------------------------------------------------------------------------

def _dist_m(a, b) -> float:
    mlat = 111320.0 * np.cos(np.radians((a[0] + b[0]) / 2))
    return float(np.hypot((a[1] - b[1]) * mlat, (a[0] - b[0]) * 111320.0))


def _nearest_idx(chain: list, target: tuple[float, float]) -> int:
    d = [
        (t[0] - target[0]) ** 2 * 1.0 + (t[1] - target[1]) ** 2 * 0.55
        for t in chain
    ]  # rough anisotropy is fine for nearest-vertex at Chicago latitudes
    return int(np.argmin(d))


def way_chain_geometry(
    spans: list[dict],
    way_geoms: dict[str, list],
    x_lo: float,
    x_hi: float,
    trim_start: tuple[float, float],
    trim_end: tuple[float, float],
    expected_len_m: float,
    gtfs_bridge=None,  # callable (a_m, b_m) -> [[lat, lon], ...] for dead ways
) -> list | None:
    """Segment polyline along OSM ways, oriented in travel direction.

    ``spans`` = the representative shape's way-cache entries. Ways overlapping
    [x_lo, x_hi] are concatenated (reversed where the shape rides the way
    backwards), the chain is trimmed at the vertices nearest the canonical
    boundary-node positions, and those exact positions are used as endpoints
    (so a direction pair sharing ways gets IDENTICAL endpoints). Returns None
    when geometry is unavailable/incoherent — caller falls back to the GTFS
    slice.
    """
    use = sorted(
        (w for w in spans if w["dist_end_m"] > x_lo and w["dist_start_m"] < x_hi),
        key=lambda w: w["dist_start_m"],
    )
    if not use:
        return None
    chain: list = []
    for w in use:
        g = way_geoms.get(str(w["way_id"]))
        if not g:
            # Way deleted/replaced upstream (OSM edited after the way cache was
            # built — e.g. remapped 6-way junctions). Bridge just this span
            # with the GTFS slice rather than abandoning the whole chain.
            if gtfs_bridge is None:
                return None
            part = [list(pt) for pt in gtfs_bridge(w["dist_start_m"], w["dist_end_m"])]
            if len(part) < 2:
                continue
        else:
            part = [list(pt) for pt in g]
            if w.get("direction") == "reverse":
                part = part[::-1]
        if not chain:
            chain = part
            continue
        # Stitch at the shared OSM junction node: routes turn at intersections,
        # so consecutive ways share an exact vertex (coords are 6-dp rounded in
        # the geom cache, so shared nodes compare equal). Cut the chain's
        # overshoot tail and the part's pre-junction head at that vertex.
        part_idx = {tuple(pt): j for j, pt in enumerate(part)}
        join = None
        for i in range(len(chain) - 1, -1, -1):
            j = part_idx.get(tuple(chain[i]))
            if j is not None:
                join = (i, j)
                break
        if join is None:
            # No shared node (rare: way split without junction) — nearest pair.
            best = None
            for i in range(max(0, len(chain) - 40), len(chain)):
                for j in range(min(40, len(part))):
                    d = _dist_m(chain[i], part[j])
                    if best is None or d < best[0]:
                        best = (d, i, j)
            if best is None or best[0] > 30.0:
                return None
            join = (best[1], best[2])
        i, j = join
        chain = chain[: i + 1] + part[j + 1 :]
    if len(chain) < 2:
        return None

    i0 = _nearest_idx(chain, trim_start)
    i1 = _nearest_idx(chain, trim_end)
    if i1 <= i0:
        return None
    trimmed = [list(trim_start)] + chain[i0 + 1 : i1] + [list(trim_end)]
    if len(trimmed) < 2:
        return None
    # sanity: chain length should resemble the route-distance span
    clen = sum(_dist_m(a, b) for a, b in zip(trimmed, trimmed[1:]))
    if expected_len_m > 50 and not (0.6 * expected_len_m < clen < 1.6 * expected_len_m):
        return None
    return trimmed


# --------------------------------------------------------------------------
# Naming from the way cache
# --------------------------------------------------------------------------

def dominant_way_attrs(
    way_spans: list[dict], x_lo: float, x_hi: float
) -> tuple[str | None, str | None]:
    """(street name, road_class) with the largest length overlap in [x_lo, x_hi]."""
    name_len: Counter = Counter()
    class_len: Counter = Counter()
    for w in way_spans:
        ov = min(x_hi, w["dist_end_m"]) - max(x_lo, w["dist_start_m"])
        if ov <= 0:
            continue
        if w.get("name"):
            name_len[w["name"]] += ov
        if w.get("road_class"):
            class_len[w["road_class"]] += ov
    name = name_len.most_common(1)[0][0] if name_len else None
    road_class = class_len.most_common(1)[0][0] if class_len else None
    return name, road_class


def segment_label(
    name: str | None, seg: Segment, node_names: dict[int, set] | None = None
) -> str:
    def cross(cp) -> str:
        if cp.cross_street_names:
            return cp.cross_street_names[0]
        if node_names:
            cands = node_names.get(cp.intersection_node_id, set()) - {name}
            if cands:
                return sorted(cands, key=len)[0]
        return "mid-block"

    span = f"{cross(seg.upstream_signal)} → {cross(seg.downstream_signal)}"
    return f"{name}: {span}" if name else span


# --------------------------------------------------------------------------
# Registry build
# --------------------------------------------------------------------------

def _dedupe_by_cluster(signals: list, canon: dict[int, int]) -> list:
    """Keep the first signal of each run of same-cluster signals (route order)."""
    out = []
    for s in signals:
        if out and canon[out[-1].intersection_node_id] == canon[s.intersection_node_id]:
            continue
        out.append(s)
    return out


def build_registry(city: CityConfig) -> dict:
    gtfs_zip = city.resolve(city.gtfs_zip)
    intersections_path = city.resolve(city.intersections_file)
    way_cache_path = city.resolve(city.way_cache_file)

    intersections = load_intersections(intersections_path)
    way_cache = json.loads(way_cache_path.read_text())
    way_geoms_path = way_cache_path.parent / "way_geoms.json"
    way_geoms = (
        json.loads(way_geoms_path.read_text()) if way_geoms_path.exists() else {}
    )
    shape_meta = shape_route_direction(gtfs_zip)
    all_bus = list_bus_shapes(gtfs_zip, city.exclude_route_prefixes)
    bus_shapes = [s for s in all_bus if s in intersections and s in shape_meta]
    skipped_no_cache = sorted(set(all_bus) - set(bus_shapes))

    stops_map = stops_by_shape(
        gtfs_zip, {s: shape_meta[s]["rep_trip_id"] for s in bus_shapes},
        city=city,
    )

    # ---- global clustering: true traffic-signal nodes --------------------
    # Boundaries come ONLY from highway=traffic_signals nodes (junction-node
    # style, or per-approach stop-line style emitted by intersections.py
    # since the 2026-07 regen). Ped signals stay demoted everywhere — the
    # earlier cluster-level "promotion" workaround for junctions whose
    # signals OSM mapped only as crossings (Milwaukee/Kimball, /Damen) is
    # gone: those junctions now carry real per-approach signal nodes.
    #
    # Per-approach signals of one junction can sit farther apart than the
    # cluster radius (~65 m across a 6-way), so each carries an anchor (its
    # nearest street-street vertex, Euclidean → identical across shapes).
    # Signal→anchor union edges plus the anchor vertices themselves as
    # cluster members (anchors of one junction are mutually near) fold every
    # approach signal into a single boundary cluster. Anchors are never
    # ``primary`` so the representative stays a real signal node.
    positions: dict[int, tuple[float, float]] = {}
    usage: Counter = Counter()
    primary: set[int] = set()
    anchor_edges: list[tuple[int, int]] = []
    anchor_pos: dict[int, tuple[float, float]] = {}
    for shape_id in bus_shapes:
        seen_in_shape = set()
        cps = intersections[shape_id]
        by_node = {}
        for cp in cps:
            by_node.setdefault(cp.intersection_node_id, cp)
        for cp in cps:
            if cp.control_type in BOUNDARY_TYPES:
                positions[cp.intersection_node_id] = (cp.lat, cp.lon)
                primary.add(cp.intersection_node_id)
                if cp.intersection_node_id not in seen_in_shape:
                    usage[cp.intersection_node_id] += 1
                    seen_in_shape.add(cp.intersection_node_id)
                a = cp.anchor_intersection_node_id
                if a is not None:
                    anchor_edges.append((cp.intersection_node_id, a))
                    ref = by_node.get(a)
                    # Anchor position if the vertex is a CP on some shape;
                    # else fall back to the signal's own position (≤ 40 m
                    # away — close enough for grid membership; the explicit
                    # edge does the actual union).
                    if ref is not None:
                        anchor_pos[a] = (ref.lat, ref.lon)
                    else:
                        anchor_pos.setdefault(a, (cp.lat, cp.lon))
    for n, pos in anchor_pos.items():
        positions.setdefault(n, pos)
    from dataio.exceptions import load_all_cluster_splits
    canon = cluster_nodes(
        positions, usage, CLUSTER_RADIUS_M, primary=primary,
        extra_edges=anchor_edges,
        split_sides=load_all_cluster_splits(),
    )

    def is_boundary_cp(cp) -> bool:
        return cp.control_type in BOUNDARY_TYPES
    node_positions = positions  # node -> (lat, lon), incl. every canonical rep
    n_geom_fallback = [0]
    n_clusters_multi = len(
        {c for c, n in Counter(canon.values()).items() if n > 1}
    )

    # ---- per-shape segmentation: canonical (new) + legacy (for traversals)
    instances: dict[str, list[dict]] = defaultdict(list)
    shape_seqs: dict[str, list[str]] = {}
    shape_bounds: dict[str, list[list]] = {}
    seg_objs: dict[str, Segment] = {}
    traversal_map: dict[str, list[list[str]]] = {}
    n_confetti_dropped = 0
    # canonical node -> street names of ways passing its position (for labels
    # at promoted junction nodes, whose crossing CPs carry no street names)
    node_names: dict[int, set] = defaultdict(set)

    for shape_id in bus_shapes:
        cps = intersections[shape_id]
        stops = stops_map.get(shape_id, [])

        # Canonical segmentation: true traffic-signal nodes, cluster-deduped
        # (per-approach signals of one junction collapse to one boundary).
        # Ped-signal CPs stay non-boundary control points.
        signals = sorted(
            (c for c in cps if is_boundary_cp(c)),
            key=lambda c: c.dist_along_route_m,
        )
        dedup = _dedupe_by_cluster(signals, canon)
        n_confetti_dropped += len(signals) - len(dedup)
        non_boundary = [c for c in cps if not is_boundary_cp(c)]
        new_segs = build_segments_from_records(
            dedup + non_boundary, stops,
            boundary_types=BOUNDARY_TYPES,
        )

        def canon_seg_id(seg: Segment) -> str:
            cu = canon[seg.upstream_signal.intersection_node_id]
            cd = canon[seg.downstream_signal.intersection_node_id]
            return f"SIG_{cu}__SIG_{cd}"

        spans_for_names = way_cache.get(shape_id, [])
        for cp in dedup:
            for w in spans_for_names:
                if w.get("name") and w["dist_start_m"] - 25 <= cp.dist_along_route_m <= w["dist_end_m"] + 25:
                    node_names[canon[cp.intersection_node_id]].add(w["name"])

        shape_seqs[shape_id] = [canon_seg_id(s) for s in new_segs]
        shape_bounds[shape_id] = [
            [canon_seg_id(s), round(s.x_start_m, 1), round(s.x_end_m, 1)] for s in new_segs
        ]
        for s in new_segs:
            cid = canon_seg_id(s)
            seg_objs.setdefault(cid, s)
            instances[cid].append(
                {
                    "shape_id": shape_id,
                    "x_start_m": s.x_start_m,
                    "x_end_m": s.x_end_m,
                    "stop_ids": [st.stop_id for st in s.stops],
                    "near_side": any(st.is_near_side for st in s.stops),
                }
            )

        # Legacy segmentation (what run_reconstruct produced) → canonical map.
        old_segs = build_segments_from_records(cps, stops)  # default boundaries
        mapping: list[list[str]] = []
        for os_ in old_segs:
            mid = (os_.x_start_m + os_.x_end_m) / 2
            new = next(
                (s for s in new_segs if s.x_start_m <= mid <= s.x_end_m), None
            )
            if new is not None:
                mapping.append([os_.seg_id, canon_seg_id(new)])
        traversal_map[shape_id] = mapping

    # Every raw member node resolves to its canonical cluster's name pool, so
    # labels see cross streets contributed by OTHER routes through the node.
    node_names_resolved: dict[int, set] = {
        n: node_names.get(c, set()) for n, c in canon.items()
    }

    # ---- canonicalize each seg_id across shapes --------------------------
    registry: dict[str, dict] = {}
    n_outlier_instances = 0
    for seg_id, insts in instances.items():
        lens = np.array([i["x_end_m"] - i["x_start_m"] for i in insts])
        med_len = float(np.median(lens))
        outliers = [
            i["shape_id"]
            for i, ln in zip(insts, lens)
            if med_len > 0 and abs(ln - med_len) / med_len > LEN_OUTLIER_FRAC
        ]
        n_outlier_instances += len(outliers)

        rep = insts[int(np.argmin(np.abs(lens - med_len)))]
        polyline, dist_m = load_gtfs_shape_with_dist(gtfs_zip, rep["shape_id"])
        if dist_m is None:
            # Same equirect ruler as every SnapToShapeMatcher fallback — the
            # x_start/x_end being sliced against were measured in that space.
            from core.mapmatch.shape_snap import equirect_cumulative_m

            dist_m = equirect_cumulative_m(polyline)
        gtfs_slice = slice_polyline(polyline, dist_m, rep["x_start_m"], rep["x_end_m"])

        # Preferred: OSM way-chain geometry trimmed at canonical node positions.
        seg0 = seg_objs[seg_id]
        canon_up_pos = node_positions.get(
            canon[seg0.upstream_signal.intersection_node_id]
        ) or (float(gtfs_slice[0][0]), float(gtfs_slice[0][1]))
        canon_down_pos = node_positions.get(
            canon[seg0.downstream_signal.intersection_node_id]
        ) or (float(gtfs_slice[-1][0]), float(gtfs_slice[-1][1]))
        geom = None
        if way_geoms:
            geom = way_chain_geometry(
                way_cache.get(rep["shape_id"], []),
                way_geoms,
                rep["x_start_m"], rep["x_end_m"],
                canon_up_pos, canon_down_pos,
                med_len,
                gtfs_bridge=lambda a, b: slice_polyline(polyline, dist_m, a, b),
            )
        geom_src = "osm" if geom is not None else "gtfs"
        if geom is None:
            geom = gtfs_slice
            n_geom_fallback[0] += 1
        geom = simplify_polyline(np.asarray(geom, dtype=float), 5.0)

        name, road_class = dominant_way_attrs(
            way_cache.get(rep["shape_id"], []), rep["x_start_m"], rep["x_end_m"]
        )

        by_route: dict[str, dict] = {}
        for i in insts:
            m = shape_meta[i["shape_id"]]
            r = by_route.setdefault(
                m["route_id"], {"route_id": m["route_id"], "direction": m["direction"], "shape_ids": []}
            )
            if i["shape_id"] not in r["shape_ids"]:
                r["shape_ids"].append(i["shape_id"])

        stop_ids = sorted({sid for i in insts for sid in i["stop_ids"]})
        seg = seg_objs[seg_id]

        # Road-strip annotations (delay-distribution viz): stop and mid-block
        # crossing positions as meters upstream of the DOWNSTREAM signal, from
        # the representative instance. Includes demoted mid-block ped signals
        # (excluded from segmentation but physically present).
        # Stops are unioned across ALL instances (offsets from each
        # instance's own downstream boundary, median across instances):
        # the rep alone can be an express variant that skips local stops
        # (2026-07-30: X4 rep hid Michigan/14th from the 14th→16th strip).
        stop_offs: dict[str, list] = defaultdict(list)
        stop_names: dict[str, str] = {}
        for inst in insts:
            for st in stops_map.get(inst["shape_id"], []):
                if inst["x_start_m"] < st["dist_along_m"] <= inst["x_end_m"]:
                    stop_offs[st["stop_id"]].append(
                        inst["x_end_m"] - st["dist_along_m"])
                    stop_names.setdefault(st["stop_id"], st["name"])
        stops_off = sorted(
            (
                {
                    "id": sid_,
                    "name": stop_names[sid_],
                    "off_m": round(float(np.median(offs)), 1),
                }
                for sid_, offs in stop_offs.items()
            ),
            key=lambda s: -s["off_m"],
        )
        crossings_off = []
        stop_signs_off = []
        for cp in intersections[rep["shape_id"]]:
            if not (rep["x_start_m"] < cp.dist_along_route_m < rep["x_end_m"]):
                continue
            if cp.control_type in ("ped_crossing_marked", "ped_crossing_signal"):
                crossings_off.append({
                    "type": cp.control_type,
                    "off_m": round(rep["x_end_m"] - cp.dist_along_route_m, 1),
                })
            elif cp.control_type == "stop":
                stop_signs_off.append({
                    "off_m": round(rep["x_end_m"] - cp.dist_along_route_m, 1),
                    "cross": cp.cross_street_names[0] if cp.cross_street_names else None,
                })

        # Minor cross-street junctions (viz: dashed centerline breaks).
        # Preferred source: "uncontrolled_junction" control points (real
        # street-street vertices — present once the intersections cache is
        # regenerated with them). Fallback: way-span boundaries, FILTERED to
        # exclude splits caused by ped crossings / crossing footways (which
        # are not street intersections).
        junctions_off = []
        junction_cps = [
            cp for cp in intersections[rep["shape_id"]]
            if cp.control_type == "uncontrolled_junction"
            and rep["x_start_m"] + 15 < cp.dist_along_route_m < rep["x_end_m"] - 15
        ]
        if junction_cps:
            for cp in junction_cps:
                off = round(rep["x_end_m"] - cp.dist_along_route_m, 1)
                if all(abs(off - j["off_m"]) > 15 for j in junctions_off):
                    junctions_off.append({
                        "off_m": off,
                        "cross": cp.cross_street_names[0] if cp.cross_street_names else None,
                    })
        else:
            ped_offs = [c["off_m"] for c in crossings_off]
            for w in way_cache.get(rep["shape_id"], []):
                x = w["dist_start_m"]
                if rep["x_start_m"] + 15 < x < rep["x_end_m"] - 15:
                    off = round(rep["x_end_m"] - x, 1)
                    if any(abs(off - pc) <= 20 for pc in ped_offs):
                        continue  # split caused by a crossing, not a street
                    if all(abs(off - j["off_m"]) > 15 for j in junctions_off):
                        junctions_off.append({"off_m": off, "cross": None})
        junctions_off.sort(key=lambda j: j["off_m"])
        up_node = canon[seg.upstream_signal.intersection_node_id]
        down_node = canon[seg.downstream_signal.intersection_node_id]

        registry[seg_id] = {
            "seg_id": seg_id,
            "up_node": up_node,
            "down_node": down_node,
            "len_m": round(med_len, 1),
            "n_instances": len(insts),
            "len_outlier_shapes": outliers,
            "geometry_lonlat": [[round(lon, 6), round(lat, 6)] for lat, lon in geom],
            "geom_src": geom_src,
            "name": name,
            "label": segment_label(name, seg, node_names_resolved),
            "road_class": road_class,
            "routes": sorted(by_route.values(), key=lambda r: r["route_id"]),
            "rev_seg_id": None,  # filled below
            "stop_ids": stop_ids,
            "n_stops": len(stop_ids),
            "has_near_side": any(i["near_side"] for i in insts),
            "stops_off": stops_off,
            "crossings_off": crossings_off,
            "stop_signs_off": stop_signs_off,
            "junctions_off": junctions_off,
        }

    for seg_id, rec in registry.items():
        rev = f"SIG_{rec['down_node']}__SIG_{rec['up_node']}"
        if rev in registry:
            rec["rev_seg_id"] = rev

    return {
        "meta": {
            "city": city.city_id,
            "intersections_sha256": sha256_of(intersections_path),
            "gtfs_zip": gtfs_zip.name,
            "boundary_types": list(BOUNDARY_TYPES),
            "cluster_radius_m": CLUSTER_RADIUS_M,
            "n_shapes": len(shape_seqs),
            "n_shapes_skipped_no_cache": len(skipped_no_cache),
            "n_segments": len(registry),
            "n_instances": sum(len(v) for v in instances.values()),
            "n_boundary_nodes": len({canon[n] for n in canon}),
            "n_alias_clusters_merged": n_clusters_multi,
            "n_anchor_union_edges": len(set(anchor_edges)),
            "n_anchored_approach_signals": len(
                {a for a, _ in set(anchor_edges)}
            ),
            "n_confetti_boundaries_dropped": n_confetti_dropped,
            "n_len_outlier_instances": n_outlier_instances,
            "n_geom_gtfs_fallback": n_geom_fallback[0],
            "n_with_reverse_twin": sum(1 for r in registry.values() if r["rev_seg_id"]),
        },
        "segments": registry,
        "shapes": {
            sid: {
                "route_id": shape_meta[sid]["route_id"],
                "direction": shape_meta[sid]["direction"],
                "seg_seq": seq,
                "seg_bounds": shape_bounds[sid],
            }
            for sid, seq in shape_seqs.items()
        },
        "traversal_map": traversal_map,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--city", default="cta")
    ap.add_argument("--out", default=None, help="default: outputs/network/<city>/segment_registry.json")
    args = ap.parse_args()

    city = get_city(args.city)
    out_path = Path(args.out) if args.out else REPO / "outputs" / "network" / city.city_id / "segment_registry.json"

    payload = build_registry(city)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload))
    m = payload["meta"]
    print(f"wrote {out_path}")
    for k, v in m.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
