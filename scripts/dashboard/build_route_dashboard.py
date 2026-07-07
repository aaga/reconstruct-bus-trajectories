"""Build the route-aggregate (average-delay) dashboard.

Mirrors ``build_dashboard.py`` for a single trip, but writes a data
payload that drives the Segments-or-Stems "delay view" instead of the
single-trip speed profile:

  * ``segments``  — per signal-to-signal segment, the mean stacked-time
    components used by ``figures/F2_corridor.png`` (free-flow,
    dwell, signals, crossings, congestion, negative residual).
  * ``features``  — same intersections/stops list as the trip view but
    annotated with ``mean_min`` and ``p95_min`` per facility, plus
    ``buffer_min = max(0, p95-mean)`` and ``attributed = p95_min >=
    0.5`` (the "Hide features without delay" filter threshold).

The segments payload comes from the existing
``out_decomposition/aggregate.csv``; the per-facility aggregates are
recomputed in-script by running the same decomposition pipeline that
``build_attribution_slides.py`` uses (``decompose_trip`` ->
``per_facility_seconds``) over every record in the bundle.

Usage:

    PYTHONPATH=src uv run python scripts/dashboard/build_route_dashboard.py \\
        --shape-id 67803936 --pattern-id 3936 --view-id sb_route
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from dataio.records_io import build_segments_for_pattern  # noqa: E402
from core.decompose import (  # noqa: E402
    build_facility_index,
    decompose_trip,
    per_facility_seconds,
)
from core.decompose.travel_time import (  # noqa: E402
    load_freeflow_table,
)
from dataio.gtfs import load_gtfs_shape_with_dist  # noqa: E402
from core.serialize import load_records  # noqa: E402

# Reuse single-trip helpers that don't depend on a trip record.
from build_dashboard import (  # noqa: E402
    ASSETS_DIR,
    DAYTIME_BUNDLE,
    FREEFLOW_TABLE,
    GTFS_ZIP,
    INTERSECTIONS_JSON,
    M_PER_MI,
    _bearing_from_polyline,
    _build_features,
    _cumulative_route_dist_m,
)

AGGREGATE_CSV = REPO / "outputs" / "out_decomposition" / "aggregate.csv"


def _build_segments_payload(
    agg_df: pd.DataFrame, segments
) -> list[dict]:
    """Project per-segment aggregate values into the JSON shape consumed
    by the Segments view. Columns are mean seconds in the CSV; the JS
    divides by 60 for display.

    The stack components match the matplotlib renderer:

      ``t_dwell_clean`` = max(0, t_dwell - t_dwell_near_signal)
        — the "confident" dwell time, drawn solid blue.
      ``t_dwell_near_signal`` — the ambiguous fraction, drawn hatched.

    Negative ``d_congestion`` (over-attribution residual) is split off
    so the JS can render it as a thin grey bar below y=0.
    """
    by_id = {row["seg_id"]: row for _, row in agg_df.iterrows()}
    out: list[dict] = []
    for s in segments:
        row = by_id.get(s.seg_id)
        if row is None:
            continue
        t_dwell = float(row["mean_t_dwell"])
        t_dwell_near = float(row["mean_t_dwell_near_signal"])
        t_dwell_clean = max(0.0, t_dwell - t_dwell_near)
        d_cong = float(row["mean_d_congestion"])
        d_cong_pos = max(0.0, d_cong)
        d_cong_neg = max(0.0, -d_cong)
        out.append({
            "seg_id": s.seg_id,
            "dist_start_m": float(s.x_start_m),
            "dist_end_m": float(s.x_end_m),
            "n_trips": int(row["n_trips"]),
            # All values are MEAN MINUTES per trip — JS scales the y axis
            # directly in min so do the /60 here once.
            "t_ff_min": float(row["mean_t_ff"]) / 60.0,
            "t_dwell_clean_min": t_dwell_clean / 60.0,
            "t_dwell_near_signal_min": t_dwell_near / 60.0,
            "d_signal_uniform_min": float(row["mean_d_signal_uniform"]) / 60.0,
            "d_signal_overflow_min": float(row["mean_d_signal_overflow"]) / 60.0,
            "d_crossing_min": float(row["mean_d_crossing"]) / 60.0,
            "d_congestion_pos_min": d_cong_pos / 60.0,
            "d_congestion_neg_min": d_cong_neg / 60.0,
        })
    return out


def _compute_per_facility_aggregates(
    facility_index, segments, ff, bundle_path: Path
) -> tuple[list[dict], int]:
    """Return (rows, n_trips) where each row carries ``facility_id``,
    ``mean_min`` and ``p95_min`` across all trips in the bundle.

    Trips that have zero seconds attributed to a facility still
    contribute to the mean (they count as 0-second samples) — same
    convention as ``compute_h_data`` in build_attribution_slides.
    """
    records = list(load_records(bundle_path))
    n_trips = len(records)
    print(f"[route]   decomposing {n_trips} trips for per-facility stats…")
    per_facility: dict[str, np.ndarray] = {
        fid: np.zeros(n_trips) for fid in facility_index
    }
    skipped = 0
    for i, rec in enumerate(records):
        try:
            decomp = decompose_trip(rec, segments, ff)
        except Exception as exc:  # noqa: BLE001
            skipped += 1
            print(f"    [{i + 1}/{n_trips}] skipping {rec.get('trip_id')}: {exc}")
            continue
        sec_per, _, _ = per_facility_seconds(decomp)
        for fid, sec in sec_per.items():
            arr = per_facility.get(fid)
            if arr is not None:
                arr[i] = sec
        if (i + 1) % 200 == 0:
            print(f"    [{i + 1}/{n_trips}]")
    if skipped:
        print(f"    skipped {skipped} trips")

    rows = []
    for fid, arr in per_facility.items():
        mean_s = float(arr.mean())
        p95_s = float(np.percentile(arr, 95))
        rows.append({
            "facility_id": fid,
            "mean_min": mean_s / 60.0,
            "p95_min": p95_s / 60.0,
        })
    return rows, n_trips


def build_route_view(
    *,
    shape_id: str,
    pattern_id: str,
    view_id: str,
    out_root: Path,
    bearing: float | None = None,
    counterpart_view_id: str | None = "sb_1001350",
) -> Path:
    # ---- shape ---------------------------------------------------------
    poly_latlon, dist_gtfs = load_gtfs_shape_with_dist(GTFS_ZIP, shape_id)
    if dist_gtfs is None:
        cumdist = _cumulative_route_dist_m(poly_latlon)
    else:
        cumdist = np.asarray(dist_gtfs, dtype=float)
    length_m = float(cumdist[-1])
    min_lat = float(poly_latlon[:, 0].min())
    max_lat = float(poly_latlon[:, 0].max())
    min_lon = float(poly_latlon[:, 1].min())
    max_lon = float(poly_latlon[:, 1].max())
    if bearing is None:
        bearing = _bearing_from_polyline(poly_latlon)

    # ---- segments + freeflow + facility index --------------------------
    segments = build_segments_for_pattern(
        pattern_id, INTERSECTIONS_JSON, GTFS_ZIP
    )
    ff = load_freeflow_table(FREEFLOW_TABLE)
    facility_index = build_facility_index(segments)

    # ---- per-segment payload from existing aggregate.csv ---------------
    if not AGGREGATE_CSV.exists():
        raise SystemExit(
            f"Missing {AGGREGATE_CSV}. Run "
            f"scripts/decomposition/run_decomposition.py first."
        )
    agg_df = pd.read_csv(AGGREGATE_CSV)
    segments_payload = _build_segments_payload(agg_df, segments)

    # ---- per-facility mean/p95 (decompose every trip) ------------------
    rows, n_trips = _compute_per_facility_aggregates(
        facility_index, segments, ff, DAYTIME_BUNDLE
    )
    row_by_fid = {r["facility_id"]: r for r in rows}

    # ---- features ------------------------------------------------------
    features = _build_features(shape_id, poly_latlon, cumdist)
    for ft in features:
        r = row_by_fid.get(ft["id"])
        if r is None:
            ft["mean_min"] = 0.0
            ft["p95_min"] = 0.0
            ft["buffer_min"] = 0.0
        else:
            ft["mean_min"] = float(r["mean_min"])
            ft["p95_min"] = float(r["p95_min"])
            ft["buffer_min"] = max(0.0, ft["p95_min"] - ft["mean_min"])
        # Map + Stems filter threshold; matches the user-set 0.5 min cut.
        ft["attributed"] = ft["p95_min"] >= 0.5

    # ---- meta ----------------------------------------------------------
    view_title = (
        f"Route 22 SB — average delay ({n_trips} daytime trips)"
    )

    payload = {
        "view_id": view_id,
        "view_title": view_title,
        "n_trips": n_trips,
        "counterpart_url": (f"../{counterpart_view_id}/"
                            if counterpart_view_id else None),
        "counterpart_label": "Switch to single trip view",
        "shape": {
            "shape_id": shape_id,
            "polyline_lonlat": [
                [float(lon), float(lat)] for lat, lon in poly_latlon
            ],
            "cumdist_m": [float(d) for d in cumdist],
            "bearing_deg": float(bearing),
            "bounds": [[min_lon, min_lat], [max_lon, max_lat]],
            "length_m": length_m,
        },
        "features": features,
        "segments": segments_payload,
    }

    # ---- write output --------------------------------------------------
    out_dir = out_root / view_id
    out_dir.mkdir(parents=True, exist_ok=True)
    for asset in sorted(ASSETS_DIR.iterdir()):
        if asset.is_file() and not asset.name.endswith("_index.html"):
            shutil.copy2(asset, out_dir / asset.name)
    shutil.copy2(ASSETS_DIR / "route_index.html", out_dir / "index.html")
    (out_dir / "data.json").write_text(json.dumps(payload, separators=(",", ":")))

    n_attr = sum(1 for f in features if f["attributed"])
    print(f"[route] wrote {out_dir}")
    print(f"[route]   shape {shape_id}: {len(poly_latlon)} vertices, "
          f"{length_m / M_PER_MI:.2f} mi, bearing {bearing:.1f}°")
    print(f"[route]   segments: {len(segments_payload)}")
    print(f"[route]   features: {len(features)} total, "
          f"{n_attr} ≥ 0.5 min p95 (attributed)")
    print(f"[route]   trips: {n_trips}")
    return out_dir


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shape-id", default="67803936")
    ap.add_argument("--pattern-id", default="3936")
    ap.add_argument("--view-id", default="sb_route")
    ap.add_argument("--out", default=str(REPO / "outputs" / "out_dashboard"))
    ap.add_argument("--bearing", type=float, default=None)
    ap.add_argument(
        "--counterpart-view-id", default="sb_1001350",
        help="view-id whose page the view-switcher button links to "
             "(default: sb_1001350). Pass empty string to disable.",
    )
    args = ap.parse_args()
    build_route_view(
        shape_id=args.shape_id,
        pattern_id=args.pattern_id,
        view_id=args.view_id,
        out_root=Path(args.out),
        bearing=args.bearing,
        counterpart_view_id=args.counterpart_view_id or None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
