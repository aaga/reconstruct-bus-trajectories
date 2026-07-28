"""Detect multi-route corridors from the segment registry.

A corridor is a maximal chain of consecutive segments (consecutive = adjacent
in at least one GTFS shape's segment sequence) that a sustained set of >=
``min_routes`` routes all traverse. Chains shorter than ``min_length_m`` are
dropped; forward chains are paired with their reverse-direction twin (via
``rev_seg_id`` overlap) into one bidirectional corridor entity.

Pure chaining logic lives in :func:`chain_corridors` (unit-tested on synthetic
registries); I/O + CLI at the bottom.

Usage:
    PYTHONPATH=src uv run python analysis/network/corridors.py --city cta
Output:
    outputs/network/<city>/corridors.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from dataio.cities import get_city  # noqa: E402

MIN_ROUTES = 2
MIN_LENGTH_M = 800.0


def chain_corridors(
    segments: dict[str, dict],
    shape_seqs: dict[str, list[str]],
    *,
    min_routes: int = MIN_ROUTES,
    min_length_m: float = MIN_LENGTH_M,
) -> list[dict]:
    """Return directed corridor chains.

    ``segments``: seg_id -> {len_m, routes: [{route_id, ...}]} (registry shape).
    ``shape_seqs``: shape_id -> ordered seg_id list.

    Greedy walk: start at any shared segment (>= min_routes) with no eligible
    predecessor (or as a branch continuation), extend along observed adjacency
    while the running intersection of route sets stays >= min_routes. At
    branches, follow the edge preserving the largest intersection; other
    branches become fresh chain starts.
    """
    routes_of = {
        sid: frozenset(r["route_id"] for r in rec["routes"])
        for sid, rec in segments.items()
    }
    shared = {sid for sid, rs in routes_of.items() if len(rs) >= min_routes}

    # Directed adjacency among shared segments, from per-shape sequences.
    succ: dict[str, set[str]] = defaultdict(set)
    pred: dict[str, set[str]] = defaultdict(set)
    for seq in shape_seqs.values():
        for a, b in zip(seq, seq[1:]):
            if a in shared and b in shared:
                succ[a].add(b)
                pred[b].add(a)

    def extendable(chain_routes: frozenset, nxt: str) -> frozenset | None:
        inter = chain_routes & routes_of[nxt]
        return inter if len(inter) >= min_routes else None

    # Chain starts: shared segments with no predecessor that could extend into
    # them, plus branch targets discovered during walks.
    starts: list[str] = sorted(
        sid
        for sid in shared
        if not any(extendable(routes_of[p] & routes_of[sid], sid) for p in pred[sid])
    )

    chains: list[dict] = []
    consumed: set[str] = set()
    queue = list(starts)
    qi = 0
    while qi < len(queue):
        start = queue[qi]
        qi += 1
        if start in consumed:
            continue
        chain = [start]
        chain_routes = routes_of[start]
        consumed.add(start)
        cur = start
        while True:
            nexts = [n for n in succ[cur] if n not in consumed]
            scored = [(n, extendable(chain_routes, n)) for n in nexts]
            viable = [(n, inter) for n, inter in scored if inter is not None]
            if not viable:
                # Dead end: any un-consumed shared successors restart chains.
                queue.extend(n for n in nexts if n in shared)
                break
            # Follow the branch preserving the most routes; others restart.
            viable.sort(key=lambda t: (-len(t[1]), t[0]))
            nxt, inter = viable[0]
            queue.extend(n for n, _ in viable[1:])
            chain.append(nxt)
            chain_routes = inter
            consumed.add(nxt)
            cur = nxt
        length = sum(segments[s]["len_m"] for s in chain)
        if length >= min_length_m:
            chains.append(
                {"seg_ids": chain, "routes": sorted(chain_routes), "len_m": round(length, 1)}
            )
    return chains


_REF_NAME = __import__("re").compile(r"^(US|IL|I|SR|M)[- ]?\d+$")


def _dominant_name(segments: dict[str, dict], seg_ids: list[str]) -> str:
    """Length-weighted dominant street name; highway refs ("US 41") only win
    when no real street name exists on the chain."""
    votes: Counter = Counter()
    ref_votes: Counter = Counter()
    for sid in seg_ids:
        rec = segments[sid]
        name = rec.get("name")
        if not name:
            continue
        (ref_votes if _REF_NAME.match(name) else votes)[name] += rec["len_m"]
    if votes:
        return votes.most_common(1)[0][0]
    if ref_votes:
        return ref_votes.most_common(1)[0][0]
    return "unnamed"


def pair_directions(chains: list[dict], segments: dict[str, dict]) -> list[dict]:
    """Group directed chains into bidirectional corridors.

    Chains join the same corridor when linked by reverse-twin segments
    (overlap >= 30% of the shorter chain, or >= 3 segments) — connected
    components over that graph. This handles asymmetric chain splits (one
    16 mi northbound chain vs two 8 mi southbound chains on the same street).
    Within a corridor, the longest chain's direction is "forward".
    """
    seg_to_chain: dict[str, int] = {}
    for ci, ch in enumerate(chains):
        for sid in ch["seg_ids"]:
            seg_to_chain[sid] = ci

    # Union-find over rev-twin links.
    parent = list(range(len(chains)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    overlap: Counter = Counter()
    for ci, ch in enumerate(chains):
        for sid in ch["seg_ids"]:
            rev = segments[sid].get("rev_seg_id")
            if rev and rev in seg_to_chain:
                cj = seg_to_chain[rev]
                if cj != ci:
                    overlap[(min(ci, cj), max(ci, cj))] += 1
    for (ci, cj), n in overlap.items():
        shorter = min(len(chains[ci]["seg_ids"]), len(chains[cj]["seg_ids"]))
        if n >= 3 or n >= 0.3 * shorter:
            union(ci, cj)

    def dir_label(chain: dict) -> str:
        votes: Counter = Counter()
        for sid in chain["seg_ids"]:
            for r in segments[sid]["routes"]:
                if r["route_id"] in chain["routes"]:
                    votes[r["direction"]] += 1
        return votes.most_common(1)[0][0] if votes else "?"

    groups: dict[int, list[int]] = defaultdict(list)
    for ci in range(len(chains)):
        groups[find(ci)].append(ci)

    corridors: list[dict] = []
    for members in groups.values():
        members.sort(key=lambda ci: -chains[ci]["len_m"])
        primary = chains[members[0]]
        fwd_dir = dir_label(primary)
        fwd_ids: list[str] = []
        rev_ids: list[str] = []
        rev_dirs: Counter = Counter()
        for ci in members:
            ch = chains[ci]
            if dir_label(ch) == fwd_dir:
                fwd_ids.extend(s for s in ch["seg_ids"] if s not in fwd_ids)
            else:
                rev_ids.extend(s for s in ch["seg_ids"] if s not in rev_ids)
                rev_dirs[dir_label(ch)] += 1
        # Backfill the reverse side with the fwd segments' reverse twins: the
        # corridor is a physical street span, so its reverse side is
        # definitionally the twins of the fwd chain — even when the reverse
        # chains were cut short or dropped during chaining (asymmetric
        # adjacency breaks previously truncated e.g. Cottage Grove NB).
        twin_order = [
            segments[s]["rev_seg_id"]
            for s in reversed(fwd_ids)
            if segments[s].get("rev_seg_id") in segments
        ]
        backfilled = [t for t in twin_order if t not in rev_ids]
        if backfilled:
            merged_rev = []
            for t in twin_order:
                if t not in merged_rev:
                    merged_rev.append(t)
            for s in rev_ids:  # keep chained-but-untwinned segs (couplets)
                if s not in merged_rev:
                    merged_rev.append(s)
            rev_ids = merged_rev
            if not rev_dirs:
                # direction label of the twins' own routes
                for t in backfilled:
                    for r in segments[t]["routes"]:
                        rev_dirs[r["direction"]] += 1
        routes = sorted({r for ci in members for r in chains[ci]["routes"]})
        corridors.append(
            {
                "cid": "",
                "name": _dominant_name(segments, fwd_ids),
                "routes": routes,
                "len_m": round(sum(segments[s]["len_m"] for s in fwd_ids), 1),
                "dir_fwd": fwd_dir,
                "seg_ids_fwd": fwd_ids,
                "dir_rev": rev_dirs.most_common(1)[0][0] if rev_ids else None,
                "seg_ids_rev": rev_ids,
            }
        )
    corridors.sort(key=lambda c: -c["len_m"])
    for i, c in enumerate(corridors):
        c["cid"] = f"cor_{i:04d}"
    return corridors


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--city", default="cta")
    ap.add_argument("--registry", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--min-routes", type=int, default=MIN_ROUTES)
    ap.add_argument("--min-length-m", type=float, default=MIN_LENGTH_M)
    args = ap.parse_args()

    city = get_city(args.city)
    reg_path = (
        Path(args.registry)
        if args.registry
        else REPO / "outputs" / "network" / city.city_id / "segment_registry.json"
    )
    out_path = (
        Path(args.out)
        if args.out
        else REPO / "outputs" / "network" / city.city_id / "corridors.json"
    )

    reg = json.loads(reg_path.read_text())
    segments = reg["segments"]
    shape_seqs = {sid: rec["seg_seq"] for sid, rec in reg["shapes"].items()}

    chains = chain_corridors(
        segments, shape_seqs, min_routes=args.min_routes, min_length_m=args.min_length_m
    )
    corridors = pair_directions(chains, segments)

    payload = {
        "meta": {
            "city": city.city_id,
            "intersections_sha256": reg["meta"]["intersections_sha256"],
            "min_routes": args.min_routes,
            "min_length_m": args.min_length_m,
            "n_directed_chains": len(chains),
            "n_corridors": len(corridors),
        },
        "corridors": corridors,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload))
    print(f"wrote {out_path}: {len(corridors)} corridors from {len(chains)} directed chains")
    for c in corridors[:15]:
        print(
            f"  {c['cid']} {c['name']:<28} {c['len_m']/1609.344:5.1f} mi  "
            f"routes={','.join(c['routes'])}  fwd={c['dir_fwd']} rev={c['dir_rev']}"
        )


if __name__ == "__main__":
    main()
