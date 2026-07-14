"""Route-aggregate (average-delay) payload for the dashboard's Average-trip view.

Computes, for the study corridor, the per-signal-to-signal-segment stacked delay
components and the per-facility mean/p95 delay across every trip in the daytime
bundle — the ``segments`` + ``features`` the Delay-per-segment view consumes.

This was previously ``scripts/dashboard/build_route_dashboard.py``, which also
emitted a now-superseded standalone front-end. Only the data computation lives
on; ``analysis.build_dashboard_data`` calls :func:`build_route_aggregate`
directly (no intermediate ``out_dashboard/.../data.json``).

Inputs (all products of the decomposition pipeline):
  * ``outputs/out_decomposition/aggregate.csv``       — per-segment means (run_decomposition)
  * ``outputs/out_decomposition/freeflow_segments.json`` — free-flow baseline
  * ``outputs/out_r2_bw5/trajectories.json``          — the daytime trip bundle
  * ``data/gtfs/cta_gtfs.zip`` + ``intersections_route22.json`` — shape/stops/signals
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

import corridor
from core.decompose import (
    build_facility_index,
    decompose_trip,
    per_facility_seconds,
)
from core.decompose.travel_time import load_freeflow_table
from core.serialize import load_records
from dataio.gtfs import load_gtfs_shape_with_dist, load_route_stops
from dataio.intersections import load_intersections
from dataio.records_io import build_segments_for_pattern
from analysis.prep.geometry import bearing_from_polyline, cumulative_route_dist_m

REPO = Path(__file__).resolve().parents[1]
GTFS_ZIP = REPO / "data" / "gtfs" / "cta_gtfs.zip"
INTERSECTIONS_JSON = REPO / corridor.INTERSECTIONS_FILE
DAYTIME_BUNDLE = REPO / "outputs" / "out_r2_bw5" / "trajectories.json"
FREEFLOW_TABLE = REPO / "outputs" / "out_decomposition" / "freeflow_segments.json"
AGGREGATE_CSV = REPO / "outputs" / "out_decomposition" / "aggregate.csv"
M_PER_MI = 1609.344


def _interp_polyline(
    poly_latlon: np.ndarray, cumdist_m: np.ndarray, dist_m: float
) -> tuple[float, float]:
    """Interpolate a (lat, lon) on the polyline at a given route distance."""
    lat = float(np.interp(dist_m, cumdist_m, poly_latlon[:, 0]))
    lon = float(np.interp(dist_m, cumdist_m, poly_latlon[:, 1]))
    return lat, lon


def _build_features(shape_id: str, poly_latlon: np.ndarray,
                    cumdist_m: np.ndarray) -> list[dict]:
    """Combine intersection ControlPoints and GTFS bus stops into one list,
    keyed by an id that matches the ``facility_id`` strings produced by
    ``attribute_event`` (so the dashboard can resolve a delay band's
    ``facility_id`` to a feature for hover-highlighting).

    facility_id conventions (from attribution.py):
      - dwell:           the GTFS stop_id (no prefix)
      - crossing:        "CX_<intersection_node_id>"
      - signal_uniform:  "SIG_<intersection_node_id>"
      - signal_overflow: "SIG_<intersection_node_id>"
      - slowdown:        None
    """
    features: list[dict] = []
    # Intersections from OSM
    cps = load_intersections(INTERSECTIONS_JSON)[shape_id]
    for cp in cps:
        if cp.control_type in ("traffic_signals", "ped_crossing_signal"):
            fid = f"SIG_{cp.intersection_node_id}"
            label_prefix = "Signal"
        elif cp.control_type == "ped_crossing_marked":
            fid = f"CX_{cp.intersection_node_id}"
            label_prefix = "Crosswalk"
        elif cp.control_type in ("stop", "give_way"):
            # Stops/yields aren't attributed by decompose_trip — keep them
            # visible on the map but use a non-clashing id namespace.
            fid = f"NODE_{cp.intersection_node_id}"
            label_prefix = "Stop sign" if cp.control_type == "stop" else "Yield"
        else:
            fid = f"NODE_{cp.intersection_node_id}"
            label_prefix = cp.control_type
        cross = " & ".join(cp.cross_street_names) if cp.cross_street_names else "mid-block"
        features.append({
            "id": fid,
            "kind": cp.control_type,
            "label": f"{label_prefix} @ {cross}",
            "cross_street": cross,           # short form, used for in-map text labels
            "lat": cp.lat,
            "lon": cp.lon,
            "dist_m": cp.dist_along_route_m,
        })

    # GTFS bus stops — lat/lon interpolated from the polyline at the stop's
    # shape_dist_traveled (close enough for marker placement; the actual stop
    # is typically a few meters offset onto the curb, which doesn't matter).
    for s in load_route_stops(GTFS_ZIP, shape_id):
        lat, lon = _interp_polyline(poly_latlon, cumdist_m, s["dist_along_m"])
        features.append({
            "id": str(s["stop_id"]),
            "kind": "bus_stop",
            "label": s["name"],
            "cross_street": s["name"],
            "lat": lat,
            "lon": lon,
            "dist_m": s["dist_along_m"],
        })
    features.sort(key=lambda f: f["dist_m"])
    return features


def _build_segments_payload(agg_df: pd.DataFrame, segments) -> list[dict]:
    """Project per-segment aggregate values into the JSON shape consumed by the
    Segments view. Columns are mean seconds in the CSV; the JS divides by 60.

      ``t_dwell_clean`` = max(0, t_dwell - t_dwell_near_signal) — confident dwell.
      ``t_dwell_near_signal`` — ambiguous fraction, drawn hatched.

    Negative ``d_congestion`` (over-attribution residual) is split off so the JS
    can render it as a thin grey bar below y=0.
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
            # All values are MEAN MINUTES per trip — JS scales the y axis in min.
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
    """Return (rows, n_trips) where each row carries ``facility_id``, ``mean_min``
    and ``p95_min`` across all trips in the bundle. Trips with zero seconds at a
    facility still contribute to the mean (0-second samples).
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
        rows.append({
            "facility_id": fid,
            "mean_min": float(arr.mean()) / 60.0,
            "p95_min": float(np.percentile(arr, 95)) / 60.0,
        })
    return rows, n_trips


def build_route_aggregate(
    shape_id: str = corridor.SHAPE_ID,
    pattern_id: str = corridor.PATTERN_ID,
) -> dict:
    """Compute the route-aggregate payload (shape + features + segments + meta)
    for the dashboard's Average-trip view. Raises ``FileNotFoundError`` if a
    decomposition input is missing."""
    # ---- shape ---------------------------------------------------------
    poly_latlon, dist_gtfs = load_gtfs_shape_with_dist(GTFS_ZIP, shape_id)
    if dist_gtfs is None:
        cumdist = cumulative_route_dist_m(poly_latlon)
    else:
        cumdist = np.asarray(dist_gtfs, dtype=float)
    length_m = float(cumdist[-1])
    min_lat = float(poly_latlon[:, 0].min())
    max_lat = float(poly_latlon[:, 0].max())
    min_lon = float(poly_latlon[:, 1].min())
    max_lon = float(poly_latlon[:, 1].max())
    bearing = bearing_from_polyline(poly_latlon)

    # ---- segments + freeflow + facility index --------------------------
    segments = build_segments_for_pattern(pattern_id, INTERSECTIONS_JSON, GTFS_ZIP)
    ff = load_freeflow_table(FREEFLOW_TABLE)
    facility_index = build_facility_index(segments)

    # ---- per-segment payload from the existing aggregate.csv -----------
    if not AGGREGATE_CSV.exists():
        raise FileNotFoundError(
            f"{AGGREGATE_CSV} — run analysis/run_decomposition.py first"
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

    view_title = f"{corridor.CORRIDOR_NAME} — average delay ({n_trips} daytime trips)"
    return {
        "view_title": view_title,
        "n_trips": n_trips,
        "shape": {
            "shape_id": shape_id,
            "polyline_lonlat": [[float(lon), float(lat)] for lat, lon in poly_latlon],
            "cumdist_m": [float(d) for d in cumdist],
            "bearing_deg": float(bearing),
            "bounds": [[min_lon, min_lat], [max_lon, max_lat]],
            "length_m": length_m,
        },
        "features": features,
        "segments": segments_payload,
    }
