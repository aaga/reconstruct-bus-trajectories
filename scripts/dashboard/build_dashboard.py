"""Build an interactive HTML dashboard for one reconstructed trip.

Combines:
  - MapLibre GL map view (bottom half, rotated so SB runs left-to-right)
  - D3 speed-profile view (top half) with delay-attribution bands

The build is purely a data-prep step: it loads the trip, runs the existing
``decompose_trip`` attribution stack (no new attribution math), and writes a
single ``data.json`` plus copies of the static JS/CSS/HTML modules from
``scripts/dashboard/assets/`` into ``out_dashboard/<view-id>/``.

Serve the output with:

    python -m http.server 8000 --directory out_dashboard

then open http://localhost:8000/<view-id>/ in a browser.

Usage:

    PYTHONPATH=src uv run python scripts/dashboard/build_dashboard.py \\
        --trip-id 1001350 \\
        --shape-id 67803936 \\
        --pattern-id 3936 \\
        --view-id sb_1001350 \\
        --bearing 90
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import asdict
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))  # so the top-level analysis/ layer is importable

from analysis.prep.geometry import (  # noqa: E402
    bearing_from_polyline as _bearing_from_polyline,
    cumulative_route_dist_m as _cumulative_route_dist_m,
)
from bus_trajectories.delay_decomposition import (  # noqa: E402
    build_segments_for_pattern,
    decompose_trip,
)
from bus_trajectories.delay_decomposition.travel_time import (  # noqa: E402
    load_freeflow_table,
)
from bus_trajectories.intersections import load_intersections  # noqa: E402
from bus_trajectories.io import (  # noqa: E402
    load_gtfs_shape_with_dist,
    load_route_stops,
)
from bus_trajectories.serialize import from_pchip_record, load_records  # noqa: E402

GTFS_ZIP = REPO / "data" / "gtfs" / "cta_gtfs.zip"
INTERSECTIONS_JSON = REPO / "intersections_route22.json"
DAYTIME_BUNDLE = REPO / "outputs" / "out_r2_bw5" / "trajectories.json"
FREEFLOW_TABLE = REPO / "outputs" / "out_decomposition" / "freeflow_segments.json"
ASSETS_DIR = REPO / "scripts" / "dashboard" / "assets"

M_PER_S_TO_MPH = 2.23694
M_PER_MI = 1609.344


# ----------------------------------------------------------------------
# Speed profile sampling — two variants: uniform in distance and
# uniform in time. Both share a (t_s ↔ dist_m) lookup table so the JS
# can convert cursor positions and zoom ranges between axes.
# ----------------------------------------------------------------------


def _sample_speed_profile(record: dict, n_points: int = 1500) -> dict:
    """Return a payload containing:

      - ``by_dist``: speed_mph sampled uniformly along route distance
      - ``by_time``: speed_mph sampled uniformly in time
      - ``xt``: a small (t_s, dist_m) lookup table for dist ↔ time
        conversion in the browser.

    The smoothed trajectory ``f`` is monotone in distance (PCHIP-enforced),
    so the inversions are well-defined.
    """
    f = from_pchip_record(record)
    t0, t1 = float(f.x[0]), float(f.x[-1])
    # 1 s grid in time is fine; a typical trip is ~60-90 min so ~3600-5400 points.
    t_dense = np.arange(t0, t1 + 1.0, 1.0)
    t_dense = np.clip(t_dense, t0, t1)
    x_dense = np.asarray(f(t_dense), dtype=float)
    v_dense_mph = np.asarray(f.derivative()(t_dense), dtype=float) * M_PER_S_TO_MPH
    # PCHIP-enforced monotonicity means duplicate x values can appear at
    # plateaus; np.interp tolerates these but assumes strictly non-decreasing
    # x. Round trips to monotone (the dups are a no-op for interpolation).
    x_dense = np.maximum.accumulate(x_dense)

    # by_dist: uniform in distance.
    dist_target = np.linspace(0.0, float(x_dense[-1]), n_points)
    speed_by_dist = np.interp(dist_target, x_dense, v_dense_mph)

    # by_time: uniform in time. Re-sample on a fresh evenly spaced grid
    # so the JS doesn't have to interpolate a 1 s array of ~5000 points.
    t_target = np.linspace(t0, t1, n_points)
    speed_by_time = np.asarray(f.derivative()(t_target), dtype=float) * M_PER_S_TO_MPH

    # xt lookup table: 800 paired (t_s, dist_m) samples. Coarse enough to
    # keep payload small, fine enough that linear interpolation in JS is
    # within ~5 m of the true trajectory.
    xt_n = 800
    t_xt = np.linspace(t0, t1, xt_n)
    x_xt = np.maximum.accumulate(np.asarray(f(t_xt), dtype=float))

    return {
        "by_dist": {
            "dist_m": dist_target.tolist(),
            "speed_mph": speed_by_dist.tolist(),
        },
        "by_time": {
            "t_s": (t_target - t0).tolist(),       # relative seconds from trip start
            "speed_mph": speed_by_time.tolist(),
        },
        "xt": {
            "t_s": (t_xt - t0).tolist(),
            "dist_m": x_xt.tolist(),
        },
        "t0_s": t0,
        "duration_s": t1 - t0,
    }


def _feature_times(features: list[dict], record: dict) -> None:
    """Annotate each feature with `t_s` — the time (in seconds since trip
    start) at which the bus crossed its `dist_m` position. Modifies in place.
    """
    f = from_pchip_record(record)
    t0, t1 = float(f.x[0]), float(f.x[-1])
    t_dense = np.arange(t0, t1 + 1.0, 1.0)
    x_dense = np.maximum.accumulate(np.asarray(f(t_dense), dtype=float))
    for ft in features:
        # np.interp clamps below x_dense[0] / above x_dense[-1] to the
        # endpoint values, which is fine for features at the route ends.
        t_at_x = float(np.interp(ft["dist_m"], x_dense, t_dense))
        ft["t_s"] = t_at_x - t0


# ----------------------------------------------------------------------
# Features list — intersections + GTFS bus stops
# ----------------------------------------------------------------------


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


# ----------------------------------------------------------------------
# Delay bands — flatten EventAttributions across all segments
# ----------------------------------------------------------------------


def _bands_from_decomp(decomp) -> list[dict]:
    """Convert every EventAttribution into a delay-band dict suitable for
    the speed-profile renderer. Bands are keyed by ``facility_id`` (matching
    the ids in the features list); slowdowns have ``facility_id = None``."""
    bands: list[dict] = []
    for seg in decomp.segments:
        for a in seg.attributions:
            ev = a.event
            cat = a.category
            # Darker shade for dwells flagged near a signal
            if cat == "dwell" and a.dwell_near_signal:
                cat = "dwell_near_signal"
            bands.append({
                "dist_start_m": float(ev.x_start),
                "dist_end_m": float(ev.x_end),
                "t_start_s": float(ev.t_start),
                "t_end_s": float(ev.t_end),
                "duration_s": float(ev.duration_s),
                "category": cat,
                "facility_id": a.facility_id,
                "seg_id": seg.seg_id,
            })
    return bands


# ----------------------------------------------------------------------
# Build a view payload + write the output directory
# ----------------------------------------------------------------------


def build_view(
    *,
    trip_id: str,
    shape_id: str,
    pattern_id: str,
    view_id: str,
    out_root: Path,
    bearing: float | None = None,
    counterpart_view_id: str | None = "sb_route",
) -> Path:
    # ---- shape ---------------------------------------------------------
    # We MUST use the GTFS-supplied per-vertex shape_dist_traveled (converted
    # to meters) as our cumdist, because both
    # `intersections.dist_along_route_m` and `load_route_stops()`'s
    # `dist_along_m` are anchored to that same coordinate system. A
    # locally-recomputed haversine cumsum disagrees by ~3-4% on Route 22
    # (route is 17,288 m by GTFS vs 17,930 m by haversine) — enough to
    # swap the visual order of features that are 20-40 m apart, like a
    # near-side bus stop and its signal.
    poly_latlon, dist_gtfs = load_gtfs_shape_with_dist(GTFS_ZIP, shape_id)
    if dist_gtfs is None:
        # Fallback for shapes that lack shape_dist_traveled; the disagreement
        # described above will surface, but at least it's internally
        # consistent (stops will be missing dist_along_m too).
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

    # ---- trip + speed profile ------------------------------------------
    records = load_records(DAYTIME_BUNDLE)
    record = next((r for r in records if str(r["trip_id"]) == str(trip_id)), None)
    if record is None:
        raise SystemExit(f"trip_id {trip_id} not in {DAYTIME_BUNDLE}")
    speed_payload = _sample_speed_profile(record)

    # ---- attribution ---------------------------------------------------
    segments = build_segments_for_pattern(
        pattern_id, INTERSECTIONS_JSON, GTFS_ZIP
    )
    ff = load_freeflow_table(FREEFLOW_TABLE)
    decomp = decompose_trip(record, segments, ff)
    bands = _bands_from_decomp(decomp)
    # The bands' t_start_s / t_end_s come out of decompose_trip in
    # absolute-trip-start-seconds. Make them relative to t0 (consistent
    # with `speed_payload["by_time"]["t_s"]` and `features[*].t_s`)
    # so the JS only has one time base to think about.
    t0 = speed_payload["t0_s"]
    for b in bands:
        b["t_start_s"] = float(b["t_start_s"]) - t0
        b["t_end_s"] = float(b["t_end_s"]) - t0

    # ---- features ------------------------------------------------------
    features = _build_features(shape_id, poly_latlon, cumdist)
    _feature_times(features, record)
    # Mark features that any delay band on this trip claims as the
    # facility_id. The "Hide features without delay" toggle hides the rest.
    attributed_ids = {b["facility_id"] for b in bands if b.get("facility_id")}
    for ft in features:
        ft["attributed"] = ft["id"] in attributed_ids

    # ---- meta ----------------------------------------------------------
    first_iso = record.get("first_ping_iso", "")
    label = f"Trip {trip_id}" + (f" · {first_iso}" if first_iso else "")
    view_title = (
        f"Route {record.get('route_id', '?')} "
        f"pattern {pattern_id} · {label}"
    )

    payload = {
        "view_id": view_id,
        "view_title": view_title,
        "counterpart_url": (f"../{counterpart_view_id}/"
                            if counterpart_view_id else None),
        "counterpart_label": "Switch to average delay view",
        "shape": {
            "shape_id": shape_id,
            # GeoJSON-style [lon, lat] order, for MapLibre.
            "polyline_lonlat": [
                [float(lon), float(lat)] for lat, lon in poly_latlon
            ],
            "cumdist_m": [float(d) for d in cumdist],
            "bearing_deg": float(bearing),
            "bounds": [[min_lon, min_lat], [max_lon, max_lat]],
            "length_m": length_m,
        },
        "features": features,
        "trajectory_xt": speed_payload["xt"],   # shared (t_s, dist_m) lookup
        "trip_duration_s": speed_payload["duration_s"],
        "views": [
            {
                "id": "primary",
                "kind": "single_trip",
                "label": label,
                "color": "#222",
                "speed_profile": {
                    "by_dist": speed_payload["by_dist"],
                    "by_time": speed_payload["by_time"],
                },
                "delay_bands": bands,
            }
        ],
    }

    # ---- write output --------------------------------------------------
    out_dir = out_root / view_id
    out_dir.mkdir(parents=True, exist_ok=True)
    # Copy every static asset file (overwrites existing on rebuild). The
    # *_index.html files are entry points owned by individual build
    # scripts — skip them here and copy trip_index.html as the entrypoint.
    for asset in sorted(ASSETS_DIR.iterdir()):
        if asset.is_file() and not asset.name.endswith("_index.html"):
            shutil.copy2(asset, out_dir / asset.name)
    shutil.copy2(ASSETS_DIR / "trip_index.html", out_dir / "index.html")
    # Write the data payload last so a stale data.json never sits next to
    # fresh assets.
    (out_dir / "data.json").write_text(json.dumps(payload, separators=(",", ":")))

    print(f"[dashboard] wrote {out_dir}")
    print(f"[dashboard]   shape {shape_id}: {len(poly_latlon)} vertices, "
          f"{length_m / M_PER_MI:.2f} mi, bearing {bearing:.1f}°")
    n_dist = len(speed_payload["by_dist"]["dist_m"])
    n_time = len(speed_payload["by_time"]["t_s"])
    print(f"[dashboard]   trip {trip_id}: {n_dist}/{n_time} speed samples "
          f"(by dist / by time), {len(bands)} delay bands")
    print(f"[dashboard]   features: {len(features)} "
          f"(signals/stops/crossings/bus stops)")
    return out_dir


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trip-id", required=True)
    ap.add_argument("--shape-id", default="67803936")
    ap.add_argument("--pattern-id", default="3936")
    ap.add_argument("--view-id", default=None,
                    help="output subdirectory name (default: sb_<trip-id>)")
    ap.add_argument("--out", default=str(REPO / "outputs" / "out_dashboard"))
    ap.add_argument(
        "--bearing", type=float, default=None,
        help="MapLibre bearing in degrees; default = computed from polyline "
             "(rotates so start→end runs left-to-right). Use 90 for SB.",
    )
    ap.add_argument(
        "--counterpart-view-id", default="sb_route",
        help="view-id whose page the view-switcher button links to "
             "(default: sb_route). Pass empty string to disable the button.",
    )
    args = ap.parse_args()
    view_id = args.view_id or f"sb_{args.trip_id}"
    build_view(
        trip_id=args.trip_id,
        shape_id=args.shape_id,
        pattern_id=args.pattern_id,
        view_id=view_id,
        out_root=Path(args.out),
        bearing=args.bearing,
        counterpart_view_id=args.counterpart_view_id or None,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
