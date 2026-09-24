"""Aggregate headway-CV moments into per-segment dashboard payloads.

Input:  outputs/network/<city>/headway_cv/service_date=*/route=*.parquet
        (delay_events --headway-cv: per 10 ft bucket, n / sum_h / sum_h2 of
        headways between consecutive same-route buses at the bucket midpoint,
        frequent network, operating hours, gaps > 60 min dropped)
Output: dashboard/data/network/cv/<sid>.json
        {"bucket_ft": 10, "routes": {"20": {"n": [...], "mean_s": [...],
         "cv": [...]}}, "window": {...}, "sha": <12>}

Moments are additive, so the whole window collapses with one GROUP BY.
CV = sigma/mu per bucket; buckets with n < MIN_N are null (thin samples make
CV explode). Mean headway ships alongside — CV 0.8 at a 4-minute headway is
a different animal than at 20 minutes.

Usage:
    PYTHONPATH=src uv run python analysis/network/build_cv.py --city cta
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

import duckdb
import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from dataio.cities import get_city  # noqa: E402

MIN_N = 25


def build(city_id: str) -> None:
    city = get_city(city_id)
    base = REPO / "outputs" / "network" / city.city_id
    registry = json.loads((base / "segment_registry.json").read_text())
    sha12 = registry["meta"]["intersections_sha256"][:12]
    seg_index = {s: i for i, s in enumerate(sorted(registry["segments"]))}
    src = base / "headway_cv"
    if not src.exists():
        raise SystemExit(f"no {src}; run delay_events --headway-cv first")

    out = REPO / "dashboard" / "data" / "network" / "cv"
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    con = duckdb.connect()
    rows = con.execute(f"""
        SELECT seg_id,
               regexp_extract(filename, 'route=([^./]+)', 1) AS route_id,
               bucket, sum(n)::BIGINT AS n,
               sum(sum_h) AS sum_h, sum(sum_h2) AS sum_h2
        FROM read_parquet('{src}/service_date=*/route=*.parquet',
                          filename=true)
        GROUP BY 1, 2, 3
    """).fetchall()
    dates = sorted(d.name.split("=")[1] for d in src.glob("service_date=*"))

    per_seg: dict[str, dict[str, dict[int, tuple]]] = {}
    for seg_id, route, b, n, sh, sh2 in rows:
        per_seg.setdefault(seg_id, {}).setdefault(route, {})[int(b)] = (
            int(n), float(sh), float(sh2))

    n_files = 0
    for seg_id, routes in per_seg.items():
        sid = seg_index.get(seg_id)
        if sid is None:
            continue
        len_ft = registry["segments"][seg_id]["len_m"] * 3.28084
        n_b = int(np.ceil(len_ft / 10.0))
        payload_routes = {}
        for route, cells in sorted(routes.items()):
            n_arr = [0] * n_b
            mean_arr: list = [None] * n_b
            cv_arr: list = [None] * n_b
            for b, (n, sh, sh2) in cells.items():
                if not (0 <= b < n_b):
                    continue
                n_arr[b] = n
                if n < MIN_N:
                    continue
                mu = sh / n
                var = max(sh2 / n - mu * mu, 0.0)
                mean_arr[b] = round(mu, 1)
                cv_arr[b] = round(float(np.sqrt(var)) / mu, 3) if mu > 0 else None
            if any(v is not None for v in cv_arr):
                payload_routes[route] = {"n": n_arr, "mean_s": mean_arr,
                                         "cv": cv_arr}
        if not payload_routes:
            continue
        (out / f"{sid}.json").write_text(json.dumps({
            "bucket_ft": 10.0,
            "routes": payload_routes,
            "window": {"start": dates[0] if dates else None,
                       "end": dates[-1] if dates else None,
                       "n_dates": len(dates)},
            "sha": sha12,
        }, separators=(",", ":")))
        n_files += 1
    print(f"wrote {n_files:,} segment CV files "
          f"({len(dates)} dates: {dates[0] if dates else '-'}"
          f" .. {dates[-1] if dates else '-'}) -> {out}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--city", default="cta")
    a = ap.parse_args()
    build(a.city)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
