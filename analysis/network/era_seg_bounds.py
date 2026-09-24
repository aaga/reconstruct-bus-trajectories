"""Map a GTFS era's shapes onto the CANONICAL segment network.

Segments are named by their bounding OSM signal nodes
(``SIG_<up>__SIG_<down>``), so they are era-independent by construction —
which is what lets one segment be charted continuously from 2024 to now.
What changes per feed era is only which shapes exist and where each
segment falls along them (``seg_bounds``).

Rebuilding ``intersections.json`` per era would mean map-matching ~1400
shapes and hitting Overpass 40 times over. Instead we project the 2,814
canonical BOUNDARY nodes onto each era polyline. Pure proximity is too
loose on its own (measured 88% precision at 8 m — signals on cross streets
land inside the radius), so the canonical segment set does the filtering:
of the ordered candidate nodes we keep the longest chain whose every
consecutive pair is a real segment. A spurious node N between A and B
would produce A__N and N__B, neither of which exists, so the chain skips
it and keeps A__B.

Output: outputs/network/<city>/era_shapes/<era>.json
    {shape_id: {route_id, direction, seg_seq, seg_bounds}}
matching the ``shapes`` block of segment_registry.json, so the batches can
swap it in per service date.

Usage:
    PYTHONPATH=src uv run python analysis/network/era_seg_bounds.py \
        --city cta --validate
    ... --all-eras
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import zipfile
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from analysis.network import gtfs_history  # noqa: E402
from dataio.cities import get_city  # noqa: E402
from dataio.gtfs import list_bus_shapes, load_gtfs_shape_with_dist  # noqa: E402

# Generous radius on purpose: the chain filter supplies precision, so the
# only cost of a loose radius is candidates the chain then skips, while a
# tight one breaks chains and loses whole runs of segments. Measured against
# the canonical registry on 150 current shapes:
#   10 m -> recall 66.3%, precision 100.0%
#   20 m -> recall 96.7%, precision  99.9%
#   30 m -> recall 99.1%, precision  99.8%   <- chosen
NEAR_M = 30.0     # boundary node counted as on-route within this of the path
DENSIFY_M = 6.0
MIN_SEG_M = 15.0  # ignore degenerate back-to-back projections


def _canonical(city):
    """(seg pair set, node -> (lat, lon)) from the canonical registry."""
    reg = json.loads((REPO / "outputs" / "network" / city.city_id
                      / "segment_registry.json").read_text())
    pairs = {}
    for seg_id, rec in reg["segments"].items():
        pairs[(int(rec["up_node"]), int(rec["down_node"]))] = seg_id
    raw = json.loads(city.resolve(city.intersections_file).read_text())
    coord: dict[int, tuple[float, float]] = {}
    for cps in raw.values():
        for d in cps:
            coord.setdefault(int(d["intersection_node_id"]),
                             (float(d["lat"]), float(d["lon"])))
    need = {n for p in pairs for n in p}
    coord = {n: c for n, c in coord.items() if n in need}
    return pairs, coord, reg


def _shape_meta(gtfs_zip: Path) -> dict[str, dict]:
    """shape_id -> {route_id, direction} from trips.txt."""
    import csv
    import io
    out: dict[str, dict] = {}
    with zipfile.ZipFile(gtfs_zip) as z, z.open("trips.txt") as f:
        for row in csv.DictReader(io.TextIOWrapper(f, "utf-8-sig")):
            sid = (row.get("shape_id") or "").strip()
            if not sid or sid in out:
                continue
            out[sid] = {
                "route_id": (row.get("route_id") or "").strip(),
                "direction": (row.get("direction") or
                              row.get("trip_headsign") or "").strip(),
            }
    return out


def _longest_chain(nodes: list[tuple[int, float]], pairs: dict) -> list:
    """Longest subsequence whose consecutive pairs are canonical segments.

    nodes is ordered by distance along the shape. O(n^2) on ~100 nodes.
    """
    n = len(nodes)
    if n < 2:
        return []
    best = [1] * n
    prev = [-1] * n
    for j in range(1, n):
        for i in range(j):
            if nodes[j][1] - nodes[i][1] < MIN_SEG_M:
                continue
            if (nodes[i][0], nodes[j][0]) in pairs and best[i] + 1 > best[j]:
                best[j] = best[i] + 1
                prev[j] = i
    end = int(np.argmax(best))
    if best[end] < 2:
        return []
    chain = []
    while end != -1:
        chain.append(nodes[end])
        end = prev[end]
    return chain[::-1]


def build_era(city, gtfs_zip: Path, pairs, coord, out_path: Path | None,
              shape_ids=None, quiet=False) -> dict:
    from scipy.spatial import cKDTree

    node_ids = np.array(sorted(coord))
    lat = np.array([coord[n][0] for n in node_ids])
    lon = np.array([coord[n][1] for n in node_ids])
    mlat = 111320.0 * np.cos(np.radians(float(lat.mean())))
    nodes_xy = np.column_stack([lon * mlat, lat * 111320.0])

    meta = _shape_meta(gtfs_zip)
    shapes = shape_ids or list_bus_shapes(gtfs_zip, city.exclude_route_prefixes)
    out: dict[str, dict] = {}
    t0 = time.time()
    for i, sid in enumerate(shapes, 1):
        try:
            poly, dist = load_gtfs_shape_with_dist(gtfs_zip, sid)
        except Exception:
            continue
        poly = np.asarray(poly, float)
        if len(poly) < 2:
            continue
        cum = (np.asarray(dist, float) if dist is not None else None)
        if cum is None:
            from core.mapmatch.shape_snap import equirect_cumulative_m
            cum = equirect_cumulative_m(poly)
        xy = np.column_stack([poly[:, 1] * mlat, poly[:, 0] * 111320.0])
        seg = np.hypot(*np.diff(xy, axis=0).T)
        pts, ds = [], []
        for k in range(len(xy) - 1):
            m = max(1, int(seg[k] // DENSIFY_M))
            t = np.linspace(0, 1, m, endpoint=False)
            pts.append(xy[k] + t[:, None] * (xy[k + 1] - xy[k]))
            ds.append(cum[k] + t * (cum[k + 1] - cum[k]))
        pts.append(xy[-1:]); ds.append(cum[-1:])
        pts = np.concatenate(pts); ds = np.concatenate(ds)
        d, idx = cKDTree(pts).query(nodes_xy, distance_upper_bound=NEAR_M)
        hit = np.isfinite(d)
        cand = sorted(((int(node_ids[j]), float(ds[idx[j]])) for j in np.nonzero(hit)[0]),
                      key=lambda r: r[1])
        chain = _longest_chain(cand, pairs)
        if len(chain) < 2:
            continue
        bounds, seq = [], []
        for a, b in zip(chain, chain[1:]):
            seg_id = pairs[(a[0], b[0])]
            bounds.append([seg_id, round(a[1], 2), round(b[1], 2)])
            seq.append(seg_id)
        m_ = meta.get(sid, {})
        out[sid] = {
            "route_id": m_.get("route_id", ""),
            "direction": m_.get("direction", ""),
            "seg_seq": seq,
            "seg_bounds": bounds,
        }
        if not quiet and i % 300 == 0:
            print(f"    [{i}/{len(shapes)}] ({time.time() - t0:.0f}s)", flush=True)
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(out))
    return out


def validate(city) -> None:
    """Re-derive CURRENT shapes and diff against the registry's seg_bounds."""
    pairs, coord, reg = _canonical(city)
    truth = reg["shapes"]
    sample = sorted(truth)[:150]
    got = build_era(city, city.resolve(city.gtfs_zip), pairs, coord, None,
                    shape_ids=sample, quiet=True)
    tp = fp = fn = 0
    dx = []
    for sid in sample:
        t = {b[0]: (b[1], b[2]) for b in truth[sid]["seg_bounds"]}
        g = {b[0]: (b[1], b[2]) for b in got.get(sid, {}).get("seg_bounds", [])}
        tp += len(set(t) & set(g)); fp += len(set(g) - set(t)); fn += len(set(t) - set(g))
        for s in set(t) & set(g):
            dx += [abs(t[s][0] - g[s][0]), abs(t[s][1] - g[s][1])]
    dx = np.array(dx) if dx else np.array([0.0])
    print(f"validation on {len(sample)} current shapes vs registry seg_bounds:")
    print(f"  segments recovered (recall) {tp / max(tp + fn, 1):.1%}  ({fn:,} missed)")
    print(f"  precision                   {tp / max(tp + fp, 1):.1%}  ({fp:,} spurious)")
    print(f"  boundary position agreement: median {np.median(dx):.1f} m, "
          f"p95 {np.percentile(dx, 95):.1f} m")


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
    pairs, coord, _ = _canonical(city)
    eras = ([a.era] if a.era else
            sorted(gtfs_history.all_eras(city)) if a.all_eras else [])
    if not eras:
        raise SystemExit("pass --validate, --era <sha8>, or --all-eras")
    base = city.resolve(city.gtfs_history_dir)
    outdir = REPO / "outputs" / "network" / city.city_id / "era_shapes"
    for e in eras:
        out = outdir / f"{e}.json"
        if out.exists() and out.stat().st_size > 0:
            print(f"{e}: cached")
            continue
        z = base / f"{e}.zip"
        if not z.exists():
            print(f"{e}: zip missing, skipping")
            continue
        t0 = time.time()
        got = build_era(city, z, pairs, coord, out, quiet=True)
        nseg = sum(len(v["seg_bounds"]) for v in got.values())
        print(f"{e}: {len(got):,} shapes, {nseg:,} seg_bounds "
              f"({time.time() - t0:.0f}s)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
