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
import math
import shutil
import sys
from dataclasses import asdict
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

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

GTFS_ZIP = REPO / "cta_gtfs.zip"
INTERSECTIONS_JSON = REPO / "intersections_route22.json"
DAYTIME_BUNDLE = REPO / "out_r2_bw5" / "trajectories.json"
FREEFLOW_TABLE = REPO / "out_decomposition" / "freeflow_segments.json"
ASSETS_DIR = REPO / "scripts" / "dashboard" / "assets"

M_PER_S_TO_MPH = 2.23694
M_PER_MI = 1609.344


# ----------------------------------------------------------------------
# Speed profile sampling — uniform in distance, not in time
# ----------------------------------------------------------------------


def _sample_speed_profile(record: dict, n_points: int = 1500
                          ) -> tuple[np.ndarray, np.ndarray]:
    """Return ``(dist_m, speed_mph)`` sampled uniformly along route distance.

    We first evaluate the smoothed trajectory ``f`` densely in time, then
    use ``np.interp`` to resample onto a uniform-distance grid. PCHIP makes
    ``f`` monotone so the inversion is well defined.
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
    dist_target = np.linspace(0.0, float(x_dense[-1]), n_points)
    speed_target = np.interp(dist_target, x_dense, v_dense_mph)
    return dist_target, speed_target


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


def _bearing_from_polyline(poly_latlon: np.ndarray) -> float:
    """MapLibre camera bearing that makes start→end run left-to-right.

    MapLibre's bearing θ means "screen-up points to compass direction θ",
    so screen-right points to (θ + 90). For the bus's motion direction
    (the compass bearing of start→end on the polyline) to render as
    screen-right, we need θ = motion_compass − 90. For SB Route 22 the
    motion is roughly south (~180°), so the camera bearing comes out at
    ~90° (top of screen = east), which is what the user asked for.
    """
    lat0, lon0 = float(poly_latlon[0, 0]), float(poly_latlon[0, 1])
    lat1, lon1 = float(poly_latlon[-1, 0]), float(poly_latlon[-1, 1])
    dlat = lat1 - lat0
    dlon = lon1 - lon0
    mlat = math.cos(math.radians((lat0 + lat1) / 2))
    # Compass bearing: 0 = north, 90 = east
    motion_compass = (math.degrees(math.atan2(dlon * mlat, dlat)) + 360.0) % 360.0
    return (motion_compass - 90.0 + 360.0) % 360.0


def build_view(
    *,
    trip_id: str,
    shape_id: str,
    pattern_id: str,
    view_id: str,
    out_root: Path,
    bearing: float | None = None,
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
    dist_m, speed_mph = _sample_speed_profile(record)

    # ---- attribution ---------------------------------------------------
    segments = build_segments_for_pattern(
        pattern_id, INTERSECTIONS_JSON, GTFS_ZIP
    )
    ff = load_freeflow_table(FREEFLOW_TABLE)
    decomp = decompose_trip(record, segments, ff)
    bands = _bands_from_decomp(decomp)

    # ---- features ------------------------------------------------------
    features = _build_features(shape_id, poly_latlon, cumdist)

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
        "views": [
            {
                "id": "primary",
                "kind": "single_trip",
                "label": label,
                "color": "#222",
                "speed_profile": {
                    "dist_m": [float(d) for d in dist_m],
                    "speed_mph": [float(v) for v in speed_mph],
                },
                "delay_bands": bands,
            }
        ],
    }

    # ---- write output --------------------------------------------------
    out_dir = out_root / view_id
    out_dir.mkdir(parents=True, exist_ok=True)
    # Copy every static asset file (overwrites existing on rebuild).
    for asset in sorted(ASSETS_DIR.iterdir()):
        if asset.is_file():
            shutil.copy2(asset, out_dir / asset.name)
    # Write the data payload last so a stale data.json never sits next to
    # fresh assets.
    (out_dir / "data.json").write_text(json.dumps(payload, separators=(",", ":")))

    print(f"[dashboard] wrote {out_dir}")
    print(f"[dashboard]   shape {shape_id}: {len(poly_latlon)} vertices, "
          f"{length_m / M_PER_MI:.2f} mi, bearing {bearing:.1f}°")
    print(f"[dashboard]   trip {trip_id}: {len(dist_m)} speed samples, "
          f"{len(bands)} delay bands")
    print(f"[dashboard]   features: {len(features)} "
          f"(signals/stops/crossings/bus stops)")
    return out_dir


def _cumulative_route_dist_m(poly_latlon: np.ndarray) -> np.ndarray:
    """Equirectangular cumulative distance along a (N,2) latlon polyline."""
    if poly_latlon.ndim != 2 or poly_latlon.shape[1] != 2:
        raise ValueError("poly_latlon must be (N, 2)")
    lat = poly_latlon[:, 0]
    lon = poly_latlon[:, 1]
    mlat_deg = 111320.0
    mlon_deg = 111320.0 * np.cos(np.radians((lat[:-1] + lat[1:]) / 2))
    dlat = (lat[1:] - lat[:-1]) * mlat_deg
    dlon = (lon[1:] - lon[:-1]) * mlon_deg
    seg_m = np.hypot(dlat, dlon)
    out = np.zeros(len(lat))
    out[1:] = np.cumsum(seg_m)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trip-id", required=True)
    ap.add_argument("--shape-id", default="67803936")
    ap.add_argument("--pattern-id", default="3936")
    ap.add_argument("--view-id", default=None,
                    help="output subdirectory name (default: sb_<trip-id>)")
    ap.add_argument("--out", default=str(REPO / "out_dashboard"))
    ap.add_argument(
        "--bearing", type=float, default=None,
        help="MapLibre bearing in degrees; default = computed from polyline "
             "(rotates so start→end runs left-to-right). Use 90 for SB.",
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
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
