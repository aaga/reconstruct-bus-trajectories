"""Network-scale per-segment free-flow travel times (canonical segments).

Pools late-night traversals (city config window) ACROSS routes per canonical
seg_id via the merged-traversal view and takes p5 (95th-percentile-fastest).
Thin segments fall down a ladder; cities with little overnight service
(``city.late_night_wide`` set — MBTA) get a widened-window step before
surrendering to the road-class prior, since the 5th-percentile-fastest over
a 20-06 window across ~95 days still approximates an empty street:

    n_night >= 20 -> p5 of late_night window       (method "p5")
    n_wide  >= 20 -> p5 of late_night_wide window  (method "p5_wide")
    n_wide  >= 8  -> p10 of late_night_wide        (method "p10_thin")
    else          -> len_m / v_class prior from same road_class ("class_prior")

(Cities without late_night_wide use the original narrow-window ladder.)

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


def _window_sql(lo: int, hi: int) -> str:
    return (
        f"(hour_local >= {lo} OR hour_local < {hi})" if lo > hi
        else f"(hour_local >= {lo} AND hour_local < {hi})"
    )


def build_freeflow(city: CityConfig, traversals_glob: str, registry: dict) -> dict:
    con = duckdb.connect()
    create_canonical_view(con, traversals_glob, registry, city)
    night = _window_sql(*city.late_night)
    wide = _window_sql(*city.late_night_wide) if city.late_night_wide else night
    rows = con.execute(
        f"""
        SELECT seg_id,
               count(*) FILTER (WHERE {night}) AS n_night,
               quantile_cont(t_obs_s, 0.05) FILTER (WHERE {night}) AS p5_night,
               count(*) AS n_wide,
               quantile_cont(t_obs_s, 0.05) AS p5_wide,
               quantile_cont(t_obs_s, 0.10) AS p10_wide
        FROM trav
        WHERE {wide} AND max_gap_in_seg_s <= {MAX_GAP_S} AND t_obs_s > 0
        GROUP BY seg_id
        """
    ).fetchall()

    segs = registry["segments"]
    out: dict[str, dict] = {}
    resolved_speeds: dict[str, list[float]] = defaultdict(list)  # road_class -> m/s
    n_by_seg: dict[str, int] = {}

    has_wide = city.late_night_wide is not None
    for seg_id, n_night, p5_night, n_wide, p5_wide, p10_wide in rows:
        n_by_seg[seg_id] = int(n_wide)
        if (n_night or 0) >= MIN_N_P5:
            t_ff, method, n = float(p5_night), "p5", int(n_night)
        elif has_wide and n_wide >= MIN_N_P5:
            t_ff, method, n = float(p5_wide), "p5_wide", int(n_wide)
        elif n_wide >= MIN_N_P10:
            # without widening, n_wide == n_night (same window)
            t_ff, method, n = float(p10_wide), "p10_thin", int(n_wide)
        else:
            continue
        out[seg_id] = {"t_ff_s": round(t_ff, 2), "n": n, "method": method}
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
        "late_night_wide": list(city.late_night_wide) if city.late_night_wide else None,
        "n_segments": len(out),
        "n_p5": sum(1 for v in out.values() if v["method"] == "p5"),
        "n_p5_wide": sum(1 for v in out.values() if v["method"] == "p5_wide"),
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
