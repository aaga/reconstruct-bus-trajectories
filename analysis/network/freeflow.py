"""Network-scale per-segment free-flow travel times (canonical segments).

Pools late-night traversals (city config window) ACROSS routes per canonical
seg_id via the merged-traversal view and takes p5 (95th-percentile-fastest).
Thin segments fall down a ladder:

    n >= 20 -> p5           (method "p5")
    n >= 8  -> p10          (method "p10_thin")
    else    -> len_m / v_class prior from same road_class  (method "class_prior")

Usage (after run_reconstruct + registry):
    PYTHONPATH=src uv run python analysis/network/freeflow.py --city cta
Output:
    outputs/network/<city>/freeflow.json   {seg_id: {t_ff_s, n, method}}
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

import duckdb
import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from analysis.network.traversals_view import create_canonical_view  # noqa: E402
from dataio.cities import CityConfig, get_city  # noqa: E402

MIN_N_P5 = 20
MIN_N_P10 = 8
MAX_GAP_S = 120.0  # tighter than aggregation: free-flow needs clean traversals


def build_freeflow(city: CityConfig, traversals_glob: str, registry: dict) -> dict:
    con = duckdb.connect()
    create_canonical_view(con, traversals_glob, registry, city)
    lo, hi = city.late_night
    night = (
        f"(hour_local >= {lo} OR hour_local < {hi})" if lo > hi
        else f"(hour_local >= {lo} AND hour_local < {hi})"
    )
    rows = con.execute(
        f"""
        SELECT seg_id, count(*) AS n,
               quantile_cont(t_obs_s, 0.05) AS p5,
               quantile_cont(t_obs_s, 0.10) AS p10
        FROM trav
        WHERE {night} AND max_gap_in_seg_s <= {MAX_GAP_S} AND t_obs_s > 0
        GROUP BY seg_id
        """
    ).fetchall()

    segs = registry["segments"]
    out: dict[str, dict] = {}
    resolved_speeds: dict[str, list[float]] = defaultdict(list)  # road_class -> m/s
    n_by_seg: dict[str, int] = {}

    for seg_id, n, p5, p10 in rows:
        n_by_seg[seg_id] = int(n)
        if n >= MIN_N_P5:
            t_ff, method = float(p5), "p5"
        elif n >= MIN_N_P10:
            t_ff, method = float(p10), "p10_thin"
        else:
            continue
        out[seg_id] = {"t_ff_s": round(t_ff, 2), "n": int(n), "method": method}
        rec = segs.get(seg_id)
        if rec and rec.get("road_class") and t_ff > 0:
            resolved_speeds[rec["road_class"]].append(rec["len_m"] / t_ff)

    v_class = {rc: float(np.median(v)) for rc, v in resolved_speeds.items() if v}
    v_all = (
        float(np.median([s for v in resolved_speeds.values() for s in v]))
        if resolved_speeds else 8.0
    )

    n_prior = 0
    for seg_id, rec in segs.items():
        if seg_id in out:
            continue
        v = v_class.get(rec.get("road_class") or "", v_all)
        out[seg_id] = {
            "t_ff_s": round(rec["len_m"] / v, 2),
            "n": n_by_seg.get(seg_id, 0),
            "method": "class_prior",
        }
        n_prior += 1

    meta = {
        "city": city.city_id,
        "intersections_sha256": registry["meta"]["intersections_sha256"],
        "late_night_window": list(city.late_night),
        "n_segments": len(out),
        "n_p5": sum(1 for v in out.values() if v["method"] == "p5"),
        "n_p10_thin": sum(1 for v in out.values() if v["method"] == "p10_thin"),
        "n_class_prior": n_prior,
        "v_class_mps": {k: round(v, 2) for k, v in sorted(v_class.items())},
    }
    return {"meta": meta, "freeflow": out}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--city", default="cta")
    args = ap.parse_args()

    city = get_city(args.city)
    base = REPO / "outputs" / "network" / city.city_id
    registry = json.loads((base / "segment_registry.json").read_text())
    glob = str(base / "traversals" / "service_date=*" / "route=*.parquet")
    payload = build_freeflow(city, glob, registry)
    out = base / "freeflow.json"
    out.write_text(json.dumps(payload))
    print(f"wrote {out}")
    print(json.dumps(payload["meta"], indent=1))


if __name__ == "__main__":
    main()
