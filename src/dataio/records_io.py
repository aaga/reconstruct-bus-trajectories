"""Disk/GTFS-backed convenience wrappers over the pure core pipeline.

These load inputs (an AVL-CSV, the GTFS shape/stops, the intersection cache) and
call the pure functions in ``core`` — so they belong in ``dataio``, not ``core``.
``core.reconstruct.reconstruct_trip`` and
``core.decompose.segments.build_segments_from_records`` stay pure and take
already-loaded records; these wrappers do the I/O.
"""

from __future__ import annotations

from pathlib import Path

from core.mapmatch import get_matcher
from core.reconstruct import TripReconstruction, reconstruct_trip
from core.decompose.segments import (
    NEAR_SIDE_THRESHOLD_M,
    Segment,
    build_segments_from_records,
)
from .gtfs import (
    load_avl_csv,
    load_gtfs_shape_with_dist,
    load_route_stops,
    shape_id_for_pattern,
)
from .intersections import load_intersections


def reconstruct_csv(
    csv_path: str | Path,
    gtfs_zip_path: str | Path,
    route_id: str,
    pattern_id: str,
    matcher_name: str = "shape_snap",
    max_perp_m: float = 50.0,
    bandwidth: int = 20,
    degree: int = 3,
) -> dict[str, TripReconstruction]:
    """Reconstruct every trip on ``route_id`` + ``pattern_id`` in ``csv_path``.

    Returns a dict keyed by ``trip_id``.
    """
    df = load_avl_csv(csv_path)
    df = df[(df["route_id"] == route_id) & (df["pattern_id"] == pattern_id)]
    if df.empty:
        raise ValueError(
            f"no rows match route_id={route_id!r}, pattern_id={pattern_id!r} in {csv_path}"
        )

    shape, dist_along = load_gtfs_shape_with_dist(
        gtfs_zip_path, shape_id_for_pattern(pattern_id)
    )
    matcher_kwargs = {"polyline_latlon": shape, "max_perp_m": max_perp_m}
    if matcher_name == "shape_snap" and dist_along is not None:
        matcher_kwargs["dist_along_m_per_vertex"] = dist_along
    matcher = get_matcher(matcher_name, **matcher_kwargs)

    out: dict[str, TripReconstruction] = {}
    for trip_id, grp in df.groupby("trip_id"):
        out[str(trip_id)] = reconstruct_trip(
            grp, matcher, bandwidth=bandwidth, degree=degree
        )
    return out


def build_segments(
    shape_id: str,
    intersections_path: Path,
    gtfs_zip_path: Path,
    *,
    near_side_threshold_m: float = NEAR_SIDE_THRESHOLD_M,
) -> list[Segment]:
    """Load ControlPoints + stops for a shape and build the segment list."""
    all_cps = load_intersections(intersections_path)
    if shape_id not in all_cps:
        raise KeyError(f"shape_id {shape_id!r} not present in {intersections_path}")
    stops = load_route_stops(gtfs_zip_path, shape_id)
    return build_segments_from_records(
        all_cps[shape_id], stops, near_side_threshold_m=near_side_threshold_m
    )


def build_segments_for_pattern(
    pattern_id: str,
    intersections_path: Path,
    gtfs_zip_path: Path,
    **kwargs,
) -> list[Segment]:
    """Convenience: resolve shape_id from pattern_id via the GTFS-derived prefix."""
    return build_segments(
        shape_id_for_pattern(pattern_id),
        intersections_path,
        gtfs_zip_path,
        **kwargs,
    )
