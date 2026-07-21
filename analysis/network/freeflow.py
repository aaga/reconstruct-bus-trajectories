"""Network-scale per-segment free-flow travel times.

Pools late-night traversals (city config window, default 22:00–05:00 local
t_enter) ACROSS routes per seg_id and takes p5 (95th-percentile-fastest),
matching the established ``segment_freeflow_table`` convention. Thin segments
fall back down a ladder:

    n >= 20 -> p5           (method "p5")
    n >= 8  -> p10          (method "p10_thin")
    else    -> len_m / v_class prior from same road_class  (method "class_prior")

Usage (after run_reconstruct has produced traversals):
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

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from dataio.cities import CityConfig, get_city  # noqa: E402

MIN_N_P5 = 20
MIN_N_P10 = 8
MAX_GAP_S = 120.0  # tighter than aggregation: free-flow needs clean traversals


def _late_night_mask(hours: pd.Series, window: tuple[int, int]) -> pd.Series:
    lo, hi = window
    return (hours >= lo) | (hours < hi) if lo > hi else (hours >= lo) & (hours < hi)


def build_freeflow(city: CityConfig, traversals_dir: Path, registry: dict) -> dict:
    files = sorted(traversals_dir.glob("service_date=*/route=*.parquet"))
    if not files:
        raise SystemExit(f"no traversal parquet under {traversals_dir}")

    cols = ["seg_id", "t_obs_s", "hour_local", "max_gap_in_seg_s", "flags"]
    samples: dict[str, list[float]] = defaultdict(list)
    for f in files:
        df = pd.read_parquet(f, columns=cols)
        df = df[
            _late_night_mask(df.hour_local, city.late_night)
            & (df.max_gap_in_seg_s <= MAX_GAP_S)
            & (df.t_obs_s > 0)
        ]
        for seg_id, grp in df.groupby("seg_id", observed=True):
            samples[seg_id].extend(grp.t_obs_s.tolist())

    segs = registry["segments"]
    out: dict[str, dict] = {}
    resolved_speeds: dict[str, list[float]] = defaultdict(list)  # road_class -> m/s

    for seg_id, ts in samples.items():
        arr = np.asarray(ts)
        if len(arr) >= MIN_N_P5:
            t_ff, method = float(np.percentile(arr, 5)), "p5"
        elif len(arr) >= MIN_N_P10:
            t_ff, method = float(np.percentile(arr, 10)), "p10_thin"
        else:
            continue  # handled by class prior below
        out[seg_id] = {"t_ff_s": round(t_ff, 2), "n": len(arr), "method": method}
        rec = segs.get(seg_id)
        if rec and rec.get("road_class") and t_ff > 0:
            resolved_speeds[rec["road_class"]].append(rec["len_m"] / t_ff)

    # Class priors: network median free-flow speed per road_class.
    v_class = {rc: float(np.median(v)) for rc, v in resolved_speeds.items() if v}
    v_all = float(np.median([s for v in resolved_speeds.values() for s in v])) if resolved_speeds else 8.0

    n_prior = 0
    for seg_id, rec in segs.items():
        if seg_id in out:
            continue
        v = v_class.get(rec.get("road_class") or "", v_all)
        out[seg_id] = {
            "t_ff_s": round(rec["len_m"] / v, 2),
            "n": len(samples.get(seg_id, [])),
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
    payload = build_freeflow(city, base / "traversals", registry)
    out = base / "freeflow.json"
    out.write_text(json.dumps(payload))
    print(f"wrote {out}")
    print(json.dumps(payload["meta"], indent=1))


if __name__ == "__main__":
    main()
