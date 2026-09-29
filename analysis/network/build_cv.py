"""Aggregate headway-CV moments into per-segment dashboard payloads.

Input:  outputs/network/<city>/headway_cv/service_date=*/route=*.parquet
        (delay_events --headway-cv: per 10 ft bucket, n / sum_h / sum_h2 of
        headways between consecutive same-route buses at the bucket midpoint,
        frequent network, operating hours, gaps > 60 min dropped)
Output: dashboard/data/network[/<city>]/cv/<sid>.json  (CTA: no <city>)
        {"bucket_ft": 10, "routes": {"20": {"n": [...], "mean_s": [...],
         "cv": [...]}}, "window": {...}, "sha": <12>}

Also cv/summary.json: per segment, the end-to-end CV change (downstream
end minus upstream end, END_FT windows) for the "Headway CV increase" map
metric — {"window": {...}, "segments": {sid: {"d_cv", "routes": {...}}}}.

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
END_FT = 50.0  # CV at a segment end = mean of valid buckets within this
# A bucket counts toward d_cv only if it saw at least this share of the
# segment's median headway count. Coverage falls off where trajectories
# stop short (e.g. the last ~40 m before a terminal: 2,276 -> 199 headways
# on MBTA route 1 at Nubian) and CV from that biased subset reads low.
MIN_COVERAGE = 0.8


def delta_cv(cv: list, n: list | None = None, end_ft: float = END_FT,
             bucket_ft: float = 10.0):
    """(cv_up, cv_down, d_cv) for one route's per-bucket CV array.

    Bucket 0 is at the DOWNSTREAM signal (delay_events lays buckets out
    from x1 back toward x0), so the downstream end is the first valid
    buckets and the upstream end the last. d_cv = cv_down - cv_up: how much
    headway irregularity grows across the segment (> 0 = bunching builds).
    With ``n`` (headways per bucket), buckets below MIN_COVERAGE of the
    segment's median count are ignored. None when either end has fewer
    than 2 valid buckets within end_ft.
    """
    k = max(int(round(end_ft / bucket_ft)), 2)
    floor = 0
    if n is not None:
        nz = [x for x in n if x]
        floor = MIN_COVERAGE * float(np.median(nz)) if nz else 0
    valid = [(i, v) for i, v in enumerate(cv)
             if v is not None and (n is None or n[i] >= floor)]
    if len(valid) < 4:
        return None
    down = [v for _, v in valid[:k]]
    up = [v for _, v in valid[-k:]]
    # the two windows must not overlap on short segments
    if valid[min(k, len(valid)) - 1][0] >= valid[-min(k, len(valid))][0]:
        half = len(valid) // 2
        down = [v for _, v in valid[:half]]
        up = [v for _, v in valid[-half:]]
    if len(down) < 2 or len(up) < 2:
        return None
    cv_down, cv_up = float(np.mean(down)), float(np.mean(up))
    return cv_up, cv_down, cv_down - cv_up


def build(city_id: str) -> None:
    city = get_city(city_id)
    base = REPO / "outputs" / "network" / city.city_id
    registry = json.loads((base / "segment_registry.json").read_text())
    sha12 = registry["meta"]["intersections_sha256"][:12]
    seg_index = {s: i for i, s in enumerate(sorted(registry["segments"]))}
    src = base / "headway_cv"
    if not src.exists():
        raise SystemExit(f"no {src}; run delay_events --headway-cv first")

    # Payload layout mirrors build_payloads: CTA at the legacy flat path,
    # other cities nested under their id (NETWORK_CITIES[city].base/cv).
    net = REPO / "dashboard" / "data" / "network"
    out = (net if city.city_id == "cta" else net / city.city_id) / "cv"
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
    summary: dict[str, dict] = {}
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
        # Map metric "Headway CV increase": per-route end-to-end change,
        # combined across routes weighted by headways observed.
        per_route = {}
        for route, rec in payload_routes.items():
            got = delta_cv(rec["cv"], rec["n"])
            if got is not None:
                per_route[route] = {"cv_up": round(got[0], 3),
                                    "cv_down": round(got[1], 3),
                                    "d_cv": round(got[2], 3),
                                    "n": int(np.median([x for x in rec["n"] if x]))}
        if per_route:
            w = np.array([r["n"] for r in per_route.values()], dtype=float)
            dv = np.array([r["d_cv"] for r in per_route.values()])
            summary[str(sid)] = {"d_cv": round(float((w * dv).sum() / w.sum()), 3),
                                 "routes": per_route}
        (out / f"{sid}.json").write_text(json.dumps({
            "bucket_ft": 10.0,
            "routes": payload_routes,
            "window": {"start": dates[0] if dates else None,
                       "end": dates[-1] if dates else None,
                       "n_dates": len(dates)},
            "sha": sha12,
        }, separators=(",", ":")))
        n_files += 1
    (out / "summary.json").write_text(json.dumps({
        "window": {"start": dates[0] if dates else None,
                   "end": dates[-1] if dates else None,
                   "n_dates": len(dates),
                   "hours_weekday": list(city.cv_hours_weekday),
                   "hours_weekend": list(city.cv_hours_weekend),
                   "daytypes": list(city.cv_daytypes)},
        "end_ft": END_FT,
        "segments": summary,
    }, separators=(",", ":")))
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
