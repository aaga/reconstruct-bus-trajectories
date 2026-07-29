"""Per-segment delay-location distributions → per-segment payload files.

Buckets classified delay-event locations (see delay_events.py) into 10 ft
bins of distance-upstream-from-the-downstream-signal, per class:

    nd   non-dwell events            (red)
    pre  pre-boarding dwell portion  (turquoise)
    post post-boarding dwell portion (purple)

One small JSON per segment (fetched on click):
    dashboard/data/network/dist/<sid>.json
    { "bucket_ft": 10, "len_ft": ..., "n_events": ...,
      "nd": [...], "pre": [...], "post": [...] }   # counts per bucket, 0-indexed
                                                   # from the downstream signal
All-data (no filters) — v1 per user decision.

Usage:
    PYTHONPATH=src uv run python analysis/network/build_distributions.py --city cta
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

BUCKET_FT = 10.0
FT_PER_M = 3.28084


def build(city_id: str) -> None:
    city = get_city(city_id)
    base = REPO / "outputs" / "network" / city.city_id
    registry = json.loads((base / "segment_registry.json").read_text())
    seg_index = {s: i for i, s in enumerate(sorted(registry["segments"]))}

    out_dir = REPO / "dashboard" / "data" / "network" / "dist"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    con = duckdb.connect()
    glob = str(base / "events" / "service_date=*" / "route=*.parquet")
    rows = con.execute(
        f"""
        SELECT seg_id, cls,
               floor(off_down_m * {FT_PER_M} / {BUCKET_FT})::INT AS bucket,
               count(*) AS n,
               sum(dur_s) AS secs
        FROM read_parquet('{glob}')
        GROUP BY 1, 2, 3
        """
    ).fetchall()

    per_seg: dict[str, dict] = {}
    for seg_id, cls, bucket, n, secs in rows:
        d = per_seg.setdefault(
            seg_id,
            {"nd": {}, "pre": {}, "post": {}, "nd_s": {}, "pre_s": {}, "post_s": {}},
        )
        d[cls][int(bucket)] = int(n)
        d[cls + "_s"][int(bucket)] = round(float(secs), 1)

    n_files = 0
    n_events_total = 0
    dates = con.execute(
        f"SELECT count(DISTINCT service_date), count(*) FROM read_parquet('{glob}')"
    ).fetchone()
    for seg_id, classes in per_seg.items():
        sid = seg_index.get(seg_id)
        if sid is None:
            continue
        rec = registry["segments"][seg_id]
        len_ft = rec["len_m"] * FT_PER_M
        n_buckets = int(np.ceil(len_ft / BUCKET_FT))
        payload = {
            "bucket_ft": BUCKET_FT,
            "len_ft": round(len_ft, 1),
            "n_dates": dates[0],
        }
        total = 0
        for cls in ("nd", "pre", "post", "nd_s", "pre_s", "post_s"):
            arr = [0] * n_buckets
            for b, n in classes[cls].items():
                if 0 <= b < n_buckets:
                    arr[b] = n
                    if not cls.endswith("_s"):
                        total += n
            payload[cls] = arr
        payload["n_events"] = total
        n_events_total += total
        (out_dir / f"{sid}.json").write_text(json.dumps(payload))
        n_files += 1

    print(f"wrote {n_files} segment distribution files "
          f"({n_events_total:,} classified events over {dates[0]} dates) → {out_dir}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--city", default="cta")
    args = ap.parse_args()
    build(args.city)


if __name__ == "__main__":
    main()
