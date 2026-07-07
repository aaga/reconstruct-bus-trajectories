"""Run the chapter-3-style delay decomposition for one trip or all trips.

Reads the daytime reconstructions in ``out_r2_bw5/trajectories.json`` and the
per-segment free-flow table at ``out_decomposition/freeflow_segments.json``,
writes per-trip JSON to ``out_decomposition/trip_<id>.json`` and an aggregate
CSV at ``out_decomposition/aggregate.csv``.

Usage:
    PYTHONPATH=src uv run python analysis/run_decomposition.py
    PYTHONPATH=src uv run python analysis/run_decomposition.py --trip-id 1001350
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
import corridor  # noqa: E402 -- centralized study-corridor constants

from core.decompose import (  # noqa: E402
    aggregate_trips,
    build_segments_for_pattern,
    decompose_trip,
)
from core.decompose.travel_time import (  # noqa: E402
    load_freeflow_table,
)
from core.serialize import load_records  # noqa: E402

PATTERN_ID = corridor.PATTERN_ID
INTERSECTIONS_JSON = REPO / corridor.INTERSECTIONS_FILE
GTFS_ZIP = REPO / "data" / "gtfs" / "cta_gtfs.zip"
DAYTIME_BUNDLE = REPO / "outputs" / "out_r2_bw5" / "trajectories.json"
OUT_DIR = REPO / "outputs" / "out_decomposition"
FF_TABLE = OUT_DIR / "freeflow_segments.json"


def _segment_to_dict(seg) -> dict:
    d = asdict(seg)
    # Drop the heavy nested EventAttribution list for the per-trip JSON — keep
    # only summary counts so the file stays small.
    cats = {
        "dwell": 0,
        "crossing": 0,
        "signal_uniform": 0,
        "signal_overflow": 0,
        "slowdown": 0,
    }
    for a in seg.attributions:
        cats[a.category] = cats.get(a.category, 0) + 1
    d["event_counts"] = cats
    d.pop("attributions", None)
    return d


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trip-id", default=None,
                    help="Decompose only this trip; default = all 431 trips")
    ap.add_argument("--out", default=str(OUT_DIR))
    ap.add_argument("--include-loss", action="store_true",
                    help="Fold accel/decel shoulders into facility buckets. "
                         "Off by default since the shoulder heuristic is "
                         "approximate.")
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    segments = build_segments_for_pattern(PATTERN_ID, INTERSECTIONS_JSON, GTFS_ZIP)
    print(f"Loaded {len(segments)} segments")

    if not FF_TABLE.exists():
        raise SystemExit(
            f"missing {FF_TABLE}; run "
            f"figures/scripts/build_freeflow_baseline.py first"
        )
    ff = load_freeflow_table(FF_TABLE)
    print(f"Loaded free-flow table: {len(ff)} segments")

    records = load_records(DAYTIME_BUNDLE)
    if args.trip_id:
        records = [r for r in records if str(r["trip_id"]) == args.trip_id]
        if not records:
            raise SystemExit(f"trip_id {args.trip_id} not in {DAYTIME_BUNDLE}")
    print(f"Decomposing {len(records)} trip(s)...")

    decomps = []
    for i, rec in enumerate(records):
        try:
            d = decompose_trip(rec, segments, ff, include_loss=args.include_loss)
        except Exception as exc:
            print(f"  [{i+1}/{len(records)}] skipping {rec.get('trip_id')}: {exc}")
            continue
        decomps.append(d)
        # Per-trip JSON.
        trip_path = out_dir / f"trip_{d.trip_id}.json"
        trip_path.write_text(json.dumps(
            {
                "trip_id": d.trip_id,
                "segments": [_segment_to_dict(s) for s in d.segments],
            },
            indent=2,
        ))
        if (i + 1) % 50 == 0 or args.trip_id:
            t_obs = sum(s.t_obs for s in d.segments) / 60
            t_dwell = sum(s.t_dwell for s in d.segments) / 60
            d_signal = sum(s.d_signal for s in d.segments) / 60
            d_cong = sum(s.d_congestion for s in d.segments) / 60
            near = sum(s.t_dwell_near_signal for s in d.segments) / 60
            print(
                f"  [{i+1}/{len(records)}] trip {d.trip_id}: "
                f"T_obs={t_obs:.1f}min, T_dwell={t_dwell:.1f}min "
                f"(near-side={near:.1f}min), "
                f"D_signal={d_signal:.1f}min, D_cong={d_cong:+.1f}min"
            )

    df = aggregate_trips(decomps)
    agg_path = out_dir / "aggregate.csv"
    df.to_csv(agg_path, index=False)
    print(f"\nWrote {agg_path} ({len(df)} segment rows from {len(decomps)} trip(s))")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
