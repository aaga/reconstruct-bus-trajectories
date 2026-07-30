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
sys.path.insert(0, str(REPO))

from analysis.network.traversals_view import create_canonical_view  # noqa: E402
from dataio.cities import get_city  # noqa: E402

BUCKET_FT = 10.0
FT_PER_M = 3.28084


def build(city_id: str) -> None:
    city = get_city(city_id)
    base = REPO / "outputs" / "network" / city.city_id
    registry = json.loads((base / "segment_registry.json").read_text())
    seg_index = {s: i for i, s in enumerate(sorted(registry["segments"]))}

    # CTA keeps the original flat location; other cities nest under their id
    # (mirrors the payload layout dashboard/data/network/<city>/).
    out_dir = (
        REPO / "dashboard" / "data" / "network" / "dist"
        if city.city_id == "cta"
        else REPO / "dashboard" / "data" / "network" / city.city_id / "dist"
    )
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    con = duckdb.connect()
    glob = str(base / "events" / "service_date=*" / "route=*.parquet")
    have_is_last = "is_last" in [
        c[0] for c in con.execute(
            f"DESCRIBE SELECT * FROM read_parquet('{glob}') LIMIT 1").fetchall()
    ]
    last_expr = ("count(*) FILTER (WHERE is_last)" if have_is_last else "0")
    rows = con.execute(
        f"""
        SELECT seg_id, cls,
               floor(off_down_m * {FT_PER_M} / {BUCKET_FT})::INT AS bucket,
               count(*) AS n,
               sum(dur_s) AS secs,
               {last_expr} AS n_last
        FROM read_parquet('{glob}')
        GROUP BY 1, 2, 3
        """
    ).fetchall()

    per_seg: dict[str, dict] = {}
    for seg_id, cls, bucket, n, secs, n_last in rows:
        d = per_seg.setdefault(
            seg_id,
            {"nd": {}, "pre": {}, "post": {}, "nd_s": {}, "pre_s": {}, "post_s": {},
             "nd_q": {}, "pre_q": {}, "post_q": {}},
        )
        d[cls][int(bucket)] = int(n)
        d[cls + "_s"][int(bucket)] = round(float(secs), 1)
        if n_last:
            d[cls + "_q"][int(bucket)] = int(n_last)

    n_files = 0
    n_events_total = 0
    dates = con.execute(
        f"SELECT count(DISTINCT service_date), count(*) FROM read_parquet('{glob}')"
    ).fetchone()

    # Traversal counts per segment over the SAME service dates as the events —
    # the denominator that turns summed delay seconds into per-trip averages.
    trav_glob = str(base / "traversals" / "service_date=*" / "route=*.parquet")
    create_canonical_view(con, trav_glob, registry, city)
    n_trips = dict(con.execute(f"""
        SELECT seg_id, count(*) FROM trav
        WHERE service_date IN (
          SELECT DISTINCT service_date FROM read_parquet('{glob}'))
        GROUP BY 1
    """).fetchall())
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
        cls_list = ["nd", "pre", "post", "nd_s", "pre_s", "post_s"]
        if have_is_last:  # queue arrays only when the batch carried the flag
            cls_list += ["nd_q", "pre_q", "post_q"]
        for cls in cls_list:
            arr = [0] * n_buckets
            for b, n in classes[cls].items():
                if 0 <= b < n_buckets:
                    arr[b] = n
                    if not cls.endswith("_s"):
                        total += n
            payload[cls] = arr
        payload["n_events"] = total
        payload["n_trips"] = int(n_trips.get(seg_id, 0))
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
