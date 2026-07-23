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
    cumulative_route_dist_m,
    simplify_polyline,
    slice_polyline,
)
from core.decompose.segments import Segment, build_segments_from_records  # noqa: E402
from dataio.cities import CityConfig, get_city  # noqa: E402
from dataio.gtfs import list_bus_shapes, load_gtfs_shape_with_dist  # noqa: E402
from dataio.intersections import load_intersections  # noqa: E402

LEN_OUTLIER_FRAC = 0.10  # instance length deviating >10% from median → flagged
BOUNDARY_TYPES = ("traffic_signals",)  # network decision: ped signals demoted
CLUSTER_RADIUS_M = 30.0  # matches intersections.DEFAULT_CLUSTER_GAP_M

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
) -> dict[int, int]:
    """Union-find nodes within ``radius_m``; return node -> canonical node.

    Canonical = the member used by the most shapes (tie: lowest node id) —
    a deterministic global rule, unlike the corridor study's per-shape
    "first in route order".
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

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    for (cx, cy), members in cell.items():
        cand: list[int] = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                cand.extend(cell.get((cx + dx, cy + dy), []))
        for i in members:
            for j in cand:
                if i < j and np.hypot(*(xy[i] - xy[j])) < radius_m:
                    union(i, j)

    groups: dict[int, list[int]] = defaultdict(list)
    for i, n in enumerate(ids):
        groups[find(i)].append(n)
    out: dict[int, int] = {}
    for members in groups.values():
        rep = max(members, key=lambda n: (usage.get(n, 0), -n))
        for n in members:
            out[n] = rep
    return out


# --------------------------------------------------------------------------
# Batched GTFS readers (one pass per file)
# --------------------------------------------------------------------------

def shape_route_direction(gtfs_zip: Path) -> dict[str, dict]:
    """One pass over trips.txt: shape_id -> {route_id, direction, rep_trip_id}."""
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
                label = _DIRECTION_ID_LABEL.get(t.get("direction_id", ""), "unknown")
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


def stops_by_shape(gtfs_zip: Path, rep_trip_by_shape: dict[str, str]) -> dict[str, list[dict]]:
    """One pass over stop_times.txt + stops.txt: shape_id -> ordered stops."""
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

    out: dict[str, list[dict]] = {}
    for trip_id, rows in rows_by_trip.items():
        rows.sort(key=lambda r: int(r["stop_sequence"]))
        stops = []
        for r in rows:
            if not r.get("shape_dist_traveled"):
                continue
            sid = r["stop_id"]
            stops.append(
                {
                    "stop_id": sid,
                    "name": stops_meta.get(sid, {}).get("stop_name", sid),
                    "dist_along_m": float(r["shape_dist_traveled"]) / 3.28084,
                }
            )
        out[trip_to_shape[trip_id]] = stops
    return out


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


def segment_label(name: str | None, seg: Segment) -> str:
    def cross(cp) -> str:
        return cp.cross_street_names[0] if cp.cross_street_names else "mid-block"

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
    shape_meta = shape_route_direction(gtfs_zip)
    bus_shapes = [s for s in list_bus_shapes(gtfs_zip) if s in intersections and s in shape_meta]
    skipped_no_cache = sorted(set(list_bus_shapes(gtfs_zip)) - set(bus_shapes))

    stops_map = stops_by_shape(
        gtfs_zip, {s: shape_meta[s]["rep_trip_id"] for s in bus_shapes}
    )

    # ---- global clustering of boundary-signal nodes ----------------------
    positions: dict[int, tuple[float, float]] = {}
    usage: Counter = Counter()
    for shape_id in bus_shapes:
        seen_in_shape = set()
        for cp in intersections[shape_id]:
            if cp.control_type in BOUNDARY_TYPES:
                positions[cp.intersection_node_id] = (cp.lat, cp.lon)
                if cp.intersection_node_id not in seen_in_shape:
                    usage[cp.intersection_node_id] += 1
                    seen_in_shape.add(cp.intersection_node_id)
    canon = cluster_nodes(positions, usage, CLUSTER_RADIUS_M)
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

    for shape_id in bus_shapes:
        cps = intersections[shape_id]
        stops = stops_map.get(shape_id, [])

        # Canonical segmentation: traffic signals only, cluster-deduped.
        signals = sorted(
            (c for c in cps if c.control_type in BOUNDARY_TYPES),
            key=lambda c: c.dist_along_route_m,
        )
        dedup = _dedupe_by_cluster(signals, canon)
        n_confetti_dropped += len(signals) - len(dedup)
        non_boundary = [c for c in cps if c.control_type not in BOUNDARY_TYPES]
        new_segs = build_segments_from_records(
            dedup + non_boundary, stops, boundary_types=BOUNDARY_TYPES
        )

        def canon_seg_id(seg: Segment) -> str:
            cu = canon[seg.upstream_signal.intersection_node_id]
            cd = canon[seg.downstream_signal.intersection_node_id]
            return f"SIG_{cu}__SIG_{cd}"

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
            dist_m = cumulative_route_dist_m(polyline)
        geom = simplify_polyline(
            slice_polyline(polyline, dist_m, rep["x_start_m"], rep["x_end_m"]), 5.0
        )

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
            "name": name,
            "label": segment_label(name, seg),
            "road_class": road_class,
            "routes": sorted(by_route.values(), key=lambda r: r["route_id"]),
            "rev_seg_id": None,  # filled below
            "stop_ids": stop_ids,
            "n_stops": len(stop_ids),
            "has_near_side": any(i["near_side"] for i in insts),
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
            "n_confetti_boundaries_dropped": n_confetti_dropped,
            "n_len_outlier_instances": n_outlier_instances,
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
