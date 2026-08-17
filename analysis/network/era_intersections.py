"""Project the canonical OSM control-point set onto a GTFS era's shapes.

``intersections.json`` is keyed by shape_id, and every historical feed uses
new shape_ids (CTA prefixes them per feed version), so a naive per-era
rebuild would mean map-matching ~1400 shapes and hitting Overpass 40 times
over — network-bound and days long.

But control points are properties of OSM NODES, not of GTFS shapes: a
traffic signal sits at the same node id and lat/lon whichever feed version
happens to describe the bus route past it. So we take the 18,713 distinct
control-point nodes already discovered across the current shapes, and for
each era shape re-derive only the shape-specific field —
``dist_along_route_m`` — by projecting those nodes onto the era polyline.
No network, no re-matching, and node ids stay identical, which is what
keeps segment ids (``SIG_<node>__SIG_<node>``) stable across all 2.5 years.

Validate before trusting: ``--validate`` re-derives the CURRENT shapes this
way and diffs against the real cache, so the approximation is measured
rather than assumed.

Usage:
    PYTHONPATH=src uv run python analysis/network/era_intersections.py \
        --city cta --validate
    ... --era <sha8>        # write caches/<city>/intersections_era_<sha8>.json
    ... --all-eras
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from analysis.network import gtfs_history  # noqa: E402
from dataio.cities import get_city  # noqa: E402
from dataio.gtfs import list_bus_shapes, load_gtfs_shape_with_dist  # noqa: E402

# A node counts as "on" a shape when the polyline passes this close. The
# canonical builder decides via way membership; 18 m reproduces it well
# (see --validate) while staying under typical parallel-street spacing.
NEAR_M = 18.0
DENSIFY_M = 8.0


def canonical_nodes(city) -> dict[int, dict]:
    """node_id -> era-independent control-point attributes."""
    raw = json.loads(city.resolve(city.intersections_file).read_text())
    attrs: dict[int, dict] = {}
    votes: dict[int, Counter] = defaultdict(Counter)
    for cps in raw.values():
        for d in cps:
            n = int(d["intersection_node_id"])
            votes[n][(d["control_type"], int(d.get("on_way_id") or 0))] += 1
            if n not in attrs:
                attrs[n] = {
                    "intersection_node_id": n,
                    "lat": float(d["lat"]),
                    "lon": float(d["lon"]),
                    "cross_street_names": d.get("cross_street_names") or [],
                    "merged_node_ids": d.get("merged_node_ids") or [],
                    "anchor_intersection_node_id":
                        d.get("anchor_intersection_node_id"),
                }
    for n, c in votes.items():
        (ct, way), _ = c.most_common(1)[0]
        attrs[n]["control_type"] = ct
        attrs[n]["on_way_id"] = way
    return attrs


def _project(poly: np.ndarray, cum: np.ndarray, tree, nodes_xy: np.ndarray,
             node_ids: np.ndarray, mlat: float):
    """Nodes near this polyline -> (node_id, dist_along_m, perp_m)."""
    xy = np.column_stack([poly[:, 1] * mlat, poly[:, 0] * 111320.0])
    # densify so the radius query can't step over a node between vertices
    pts, ds = [], []
    seg = np.hypot(*np.diff(xy, axis=0).T)
    for i in range(len(xy) - 1):
        k = max(1, int(seg[i] // DENSIFY_M))
        t = np.linspace(0, 1, k, endpoint=False)
        pts.append(xy[i] + t[:, None] * (xy[i + 1] - xy[i]))
        ds.append(cum[i] + t * (cum[i + 1] - cum[i]))
    pts.append(xy[-1:]); ds.append(cum[-1:])
    pts = np.concatenate(pts); ds = np.concatenate(ds)
    from scipy.spatial import cKDTree
    ptree = cKDTree(pts)
    hits = ptree.query_ball_point(nodes_xy, r=NEAR_M)
    out = []
    for j, idxs in enumerate(hits):
        if not idxs:
            continue
        idxs = np.asarray(idxs)
        d2 = np.hypot(*(pts[idxs] - nodes_xy[j]).T)
        b = idxs[int(np.argmin(d2))]
        out.append((int(node_ids[j]), float(ds[b]), float(d2.min())))
    out.sort(key=lambda r: r[1])
    return out


def build_era(city, gtfs_zip: Path, out_path: Path | None,
              shape_ids: list[str] | None = None) -> dict:
    from scipy.spatial import cKDTree  # noqa: F401  (imported in _project)

    attrs = canonical_nodes(city)
    node_ids = np.array(sorted(attrs))
    lat = np.array([attrs[n]["lat"] for n in node_ids])
    lon = np.array([attrs[n]["lon"] for n in node_ids])
    mlat = 111320.0 * np.cos(np.radians(float(lat.mean())))
    nodes_xy = np.column_stack([lon * mlat, lat * 111320.0])
    tree = None

    shapes = shape_ids or list_bus_shapes(gtfs_zip, city.exclude_route_prefixes)
    payload: dict[str, list] = {}
    t0 = time.time()
    for i, sid in enumerate(shapes, 1):
        try:
            poly, dist = load_gtfs_shape_with_dist(gtfs_zip, sid)
        except Exception:
            continue
        poly = np.asarray(poly, float)
        if len(poly) < 2:
            continue
        if dist is None:
            from core.mapmatch.shape_snap import equirect_cumulative_m
            cum = equirect_cumulative_m(poly)
        else:
            cum = np.asarray(dist, float)
        rows = _project(poly, cum, tree, nodes_xy, node_ids, mlat)
        payload[sid] = [
            {**{k: v for k, v in attrs[n].items()},
             "dist_along_route_m": round(d, 2)}
            for n, d, _p in rows
        ]
        if i % 200 == 0:
            print(f"    [{i}/{len(shapes)}] shapes ({time.time() - t0:.0f}s)",
                  flush=True)
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload))
    return payload


def validate(city) -> None:
    """Re-derive CURRENT shapes by projection; diff vs the real cache."""
    truth = json.loads(city.resolve(city.intersections_file).read_text())
    gtfs = city.resolve(city.gtfs_zip)
    sample = sorted(truth)[:120]
    got = build_era(city, gtfs, None, shape_ids=sample)
    BT = "traffic_signals"
    tp = fp = fn = 0
    dd = []
    for sid in sample:
        t = {int(c["intersection_node_id"]): float(c["dist_along_route_m"])
             for c in truth[sid] if c["control_type"] == BT}
        g = {c["intersection_node_id"]: c["dist_along_route_m"]
             for c in got.get(sid, []) if c["control_type"] == BT}
        tp += len(set(t) & set(g)); fp += len(set(g) - set(t)); fn += len(set(t) - set(g))
        dd += [abs(t[n] - g[n]) for n in set(t) & set(g)]
    dd = np.array(dd) if dd else np.array([0.0])
    print(f"validation on {len(sample)} current shapes (traffic_signals only):")
    print(f"  recall    {tp / max(tp + fn, 1):.1%}  ({fn:,} missed)")
    print(f"  precision {tp / max(tp + fp, 1):.1%}  ({fp:,} spurious)")
    print(f"  dist_along agreement: median {np.median(dd):.1f} m, "
          f"p95 {np.percentile(dd, 95):.1f} m, max {dd.max():.1f} m")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--city", default="cta")
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--era", default=None)
    ap.add_argument("--all-eras", action="store_true")
    a = ap.parse_args()
    city = get_city(a.city)
    if a.validate:
        validate(city)
        return 0
    eras = ([a.era] if a.era else
            sorted(gtfs_history.all_eras(city)) if a.all_eras else [])
    if not eras:
        raise SystemExit("pass --validate, --era <sha8>, or --all-eras")
    base = city.resolve(city.gtfs_history_dir)
    for e in eras:
        out = city.resolve(city.intersections_file).parent / f"intersections_era_{e}.json"
        if out.exists() and out.stat().st_size > 0:
            print(f"{e}: cached")
            continue
        print(f"{e}: projecting...", flush=True)
        t0 = time.time()
        p = build_era(city, base / f"{e}.zip", out)
        print(f"{e}: {len(p):,} shapes ({time.time() - t0:.0f}s) -> {out.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
