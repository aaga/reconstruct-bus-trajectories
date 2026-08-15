"""Extract Overpass-equivalent way/node data from a local OSM PBF extract.

Replaces the public-Overpass fetch in the intersections/way-geometry
pipelines. The PBF is the SAME file the Valhalla tileset is built from, so
route matching (stage 1) and intersection enrichment (stage 2) see one OSM
vintage by construction — the mixed-vintage failure mode (2026-07: stale
way ids, missing per-approach signals) cannot recur.

``extract_overpass_payload`` replicates the named-set Overpass query used by
``dataio.intersections.query_overpass``::

    way(id:...) -> .bus_ways;
    node(w.bus_ways) -> .bus_nodes;
    way(bn.bus_nodes) -> .all_ways;      # every way touching a bus node
    node(w.all_ways) -> .all_nodes;
    (.all_ways; .all_nodes;); out body;

as three streaming passes over the PBF (ways → ways → nodes; PBFs store
nodes first, so the node pass runs on a fresh reader). Each pass is a few
seconds per state extract with the C++ osmium core.

Output shape matches Overpass ``out body`` JSON:
    {"elements": [{"type": "way", "id", "nodes": [...], "tags": {...}},
                  {"type": "node", "id", "lat", "lon", "tags": {...}}, ...]}
"""

from __future__ import annotations

from pathlib import Path

import osmium


def _way_pass(pbf_path: str | Path, keep) -> list[dict]:
    """One ways-only pass; ``keep(way) -> bool`` selects emitted ways."""
    out: list[dict] = []

    class H(osmium.SimpleHandler):
        def way(self, w):
            if keep(w):
                out.append({
                    "type": "way",
                    "id": w.id,
                    "nodes": [n.ref for n in w.nodes],
                    "tags": dict(w.tags),
                })

    h = H()
    h.apply_file(str(pbf_path))
    return out


def _node_pass(pbf_path: str | Path, node_ids: set[int]) -> list[dict]:
    out: list[dict] = []

    class H(osmium.SimpleHandler):
        def node(self, n):
            if n.id in node_ids:
                out.append({
                    "type": "node",
                    "id": n.id,
                    "lat": n.location.lat,
                    "lon": n.location.lon,
                    "tags": dict(n.tags),
                })

    h = H()
    h.apply_file(str(pbf_path))
    return out


def extract_overpass_payload(
    pbf_path: str | Path, way_ids: list[int] | set[int], progress: bool = True
) -> dict:
    """Overpass-shaped payload for ``way_ids`` + every way sharing a node."""
    targets = {int(w) for w in way_ids}

    def log(msg: str) -> None:
        if progress:
            print(f"[pbf] {msg}", flush=True)

    log(f"pass 1/3: {len(targets):,} target ways ← {Path(pbf_path).name}")
    bus_ways = _way_pass(pbf_path, lambda w: w.id in targets)
    bus_nodes = {n for w in bus_ways for n in w["nodes"]}
    missing = targets - {w["id"] for w in bus_ways}
    if missing:
        log(f"  WARNING: {len(missing)} target way(s) not in extract "
            f"(first few: {sorted(missing)[:5]})")

    log(f"pass 2/3: ways touching {len(bus_nodes):,} bus nodes")
    bus_ids = {w["id"] for w in bus_ways}
    cross_ways = _way_pass(
        pbf_path,
        lambda w: w.id not in bus_ids and any(n.ref in bus_nodes for n in w.nodes),
    )
    all_ways = bus_ways + cross_ways
    all_nodes = {n for w in all_ways for n in w["nodes"]}

    log(f"pass 3/3: {len(all_nodes):,} member nodes")
    nodes = _node_pass(pbf_path, all_nodes)

    log(f"done: {len(all_ways):,} ways, {len(nodes):,} nodes")
    return {"elements": all_ways + nodes}


def extract_way_geoms(
    pbf_path: str | Path, way_ids: list[int] | set[int], progress: bool = True
) -> dict[int, list[list[float]]]:
    """{way_id: [[lat, lon], ...]} for each way (empty list if not in extract)."""
    targets = {int(w) for w in way_ids}
    ways = _way_pass(pbf_path, lambda w: w.id in targets)
    need = {n for w in ways for n in w["nodes"]}
    if progress:
        print(f"[pbf] way-geoms: {len(ways):,}/{len(targets):,} ways, "
              f"{len(need):,} nodes", flush=True)
    coords = {
        n["id"]: [round(n["lat"], 6), round(n["lon"], 6)]
        for n in _node_pass(pbf_path, need)
    }
    out: dict[int, list[list[float]]] = {w: [] for w in targets}
    for w in ways:
        pts = [coords[n] for n in w["nodes"] if n in coords]
        out[w["id"]] = pts
    return out
