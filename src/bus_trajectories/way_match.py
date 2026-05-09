"""Map a GTFS shape to its sequence of OSM ways via Valhalla /trace_attributes.

For every GTFS shape we POST the polyline to Valhalla, then derive an ordered
list of ``WaySegment`` rows: each is one OSM way's appearance in the route,
with ``dist_start_m``/``dist_end_m`` anchored to the shape's
``shape_dist_traveled`` ruler — the same coordinate system the trajectory
pipeline uses, so projected features and bus distances agree by construction.

Algorithm — "shape-walk tiling":

  Valhalla returns three things we need:
    1. ``edges`` — the route's edge sequence, each carrying way_id + metadata.
    2. ``shape`` — the encoded matched-route polyline (polyline6).
    3. Per edge, ``begin_shape_index``/``end_shape_index`` — where in the
       shape polyline this edge starts and ends.

  Critically, consecutive edges chain perfectly: ``edges[i].end_shape_index
  == edges[i+1].begin_shape_index``, so the matched polyline is partitioned
  into edges with no gaps and no overlaps. We exploit that:

    1. Decode ``shape`` into a list of (lat, lon) vertices.
    2. Project each shape vertex onto the GTFS polyline using
       :class:`SnapToShapeMatcher` — gives ``gtfs_dist`` per shape vertex.
    3. For each edge, ``dist_start_m = gtfs_dist[begin_shape_index]`` and
       ``dist_end_m = gtfs_dist[end_shape_index]``. Adjacent edges share
       boundaries → automatic gap-free, overlap-free tiling.
    4. Clamp first segment start to 0 and last segment end to shape_length
       to handle Valhalla's source_percent_along/target_percent_along
       (which trim the trace to the actual route start/end).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

import numpy as np

from .io import list_bus_shapes, load_gtfs_shape_with_dist
from .mapmatch.shape_snap import SnapToShapeMatcher


# Valhalla's `trace_attributes` filter list. `edge.oneway` is NOT a valid
# attribute; direction information comes from `edge.forward`. ``shape`` and
# ``edge.begin/end_shape_index`` are the load-bearing fields for tiling.
_VALHALLA_FILTERS = (
    "edge.way_id",
    "edge.length",
    "edge.names",
    "edge.forward",
    "edge.road_class",
    "edge.use",
    "edge.lane_count",
    "edge.speed",
    "edge.begin_shape_index",
    "edge.end_shape_index",
    "edge.source_percent_along",
    "edge.target_percent_along",
    "matched.type",
    "matched.edge_index",
    "matched.point",
    "matched.distance_along_edge",
    "matched.distance_from_trace_point",
    "shape",
    "shape_attributes.length",
)

# Valhalla emits 2**64-1 for matched_points.edge_index when there is no edge
# match for that input vertex.
_UNMATCHED = 2**64 - 1


@dataclass(frozen=True)
class WaySegment:
    way_id: int
    dist_start_m: float
    dist_end_m: float
    direction: str            # "forward" / "reverse" / "unknown" (relative to OSM way's intrinsic node order)
    name: str | None
    road_class: str | None


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ValhallaError(RuntimeError):
    """Generic Valhalla error (HTTP non-2xx, malformed JSON, error response)."""


class ValhallaUnreachable(ValhallaError):
    """Endpoint not reachable (connection refused, DNS, TLS, timeout)."""


# ---------------------------------------------------------------------------
# HTTP wrapper
# ---------------------------------------------------------------------------


def call_valhalla(
    polyline_latlon: np.ndarray,
    *,
    endpoint: str = "http://localhost:8002",
    costing: str = "bus",
    timeout_s: float = 60.0,
) -> dict:
    """POST a polyline to ``/trace_attributes`` and return parsed JSON."""
    if polyline_latlon.ndim != 2 or polyline_latlon.shape[1] != 2:
        raise ValueError("polyline_latlon must have shape (N, 2)")
    if polyline_latlon.shape[0] < 2:
        raise ValueError("polyline_latlon must have >= 2 vertices")

    body = {
        "shape": [
            {"lat": float(lat), "lon": float(lon)}
            for lat, lon in polyline_latlon.tolist()
        ],
        "shape_match": "map_snap",
        "costing": costing,
        # ``verbose: true`` is required to get `matched.point` (snapped lat/lon),
        # `distance_from_trace_point`, and the encoded route polyline back.
        "verbose": True,
        "filters": {"attributes": list(_VALHALLA_FILTERS), "action": "include"},
    }
    url = endpoint.rstrip("/") + "/trace_attributes"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json",
                 "User-Agent": "bus-trajectories/way-match"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body_text = ""
        try:
            body_text = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise ValhallaError(
            f"Valhalla HTTP {e.code} {e.reason}: {body_text[:500]}"
        ) from e
    except urllib.error.URLError as e:
        raise ValhallaUnreachable(
            f"could not reach Valhalla at {url}: {e.reason}"
        ) from e


# ---------------------------------------------------------------------------
# Response → WaySegment list
# ---------------------------------------------------------------------------


def _is_matched(edge_index: object) -> bool:
    if edge_index is None:
        return False
    try:
        ei = int(edge_index)
    except (TypeError, ValueError):
        return False
    return 0 <= ei < _UNMATCHED


def _direction_of(edge: dict) -> str:
    if "forward" not in edge:
        return "unknown"
    return "forward" if edge["forward"] else "reverse"


def decode_polyline6(s: str) -> list[tuple[float, float]]:
    """Decode a Valhalla-style polyline6 string into a list of ``(lat, lon)``.

    Same as Google's polyline algorithm but with 1e6 precision (rather than
    1e5). The matched-route ``shape`` field uses this encoding.
    """
    coords: list[tuple[float, float]] = []
    i = lat = lon = 0
    while i < len(s):
        shift = result = 0
        while True:
            b = ord(s[i]) - 63
            i += 1
            result |= (b & 0x1f) << shift
            shift += 5
            if b < 0x20:
                break
        dlat = ~(result >> 1) if result & 1 else result >> 1
        lat += dlat
        shift = result = 0
        while True:
            b = ord(s[i]) - 63
            i += 1
            result |= (b & 0x1f) << shift
            shift += 5
            if b < 0x20:
                break
        dlon = ~(result >> 1) if result & 1 else result >> 1
        lon += dlon
        coords.append((lat / 1e6, lon / 1e6))
    return coords


def extract_segments(
    response: dict,
    polyline_latlon: np.ndarray,
    dist_along_m_per_vertex: np.ndarray,
    *,
    max_perp_m: float = 200.0,
) -> list[WaySegment]:
    """Build a gap-free, overlap-free tiling of WaySegments from a Valhalla response.

    Walks the encoded matched-route polyline (``response["shape"]``), projects
    each polyline vertex onto the GTFS polyline to convert from
    matched-route coordinates to GTFS ``shape_dist_traveled`` coordinates,
    then slices the route into one segment per edge using
    ``begin/end_shape_index``. Adjacent edges chain perfectly because their
    shape indices share boundaries.

    Parameters
    ----------
    response :
        Parsed Valhalla ``trace_attributes`` response. Must include ``shape``
        and per-edge ``begin/end_shape_index`` (request via ``verbose: true``).
    polyline_latlon :
        The ``(N, 2)`` GTFS polyline that was sent to Valhalla — needed to
        snap matched-shape vertices back onto the GTFS-anchored ruler.
    dist_along_m_per_vertex :
        Per-GTFS-vertex ``shape_dist_traveled`` in meters.
    max_perp_m :
        Snap tolerance for matched-shape vertex → GTFS polyline projection.
        Should be permissive (>= 50 m) since matched routes can deviate a
        little from the GTFS centerline at curves; we just need a working
        projection.
    """
    edges = response.get("edges") or []
    shape_str = response.get("shape") or ""
    if not edges or not shape_str:
        return []

    shape_pts = decode_polyline6(shape_str)
    if not shape_pts:
        return []

    shape_length_m = (
        float(dist_along_m_per_vertex[-1]) if len(dist_along_m_per_vertex) else 0.0
    )

    # Project the matched-route shape vertices onto the GTFS polyline so we
    # can express each vertex's position in GTFS shape_dist_traveled space.
    matcher = SnapToShapeMatcher(
        polyline_latlon,
        max_perp_m=max_perp_m,
        dist_along_m_per_vertex=dist_along_m_per_vertex,
    )
    lats = np.array([p[0] for p in shape_pts])
    lons = np.array([p[1] for p in shape_pts])
    proj = matcher.match(lats, lons)
    gtfs_dist_at_shape = proj.dist_along_m

    n_shape = len(shape_pts)
    segments: list[WaySegment] = []
    for edge in edges:
        if "way_id" not in edge:
            continue
        bi = edge.get("begin_shape_index")
        ei = edge.get("end_shape_index")
        if bi is None or ei is None:
            continue
        bi_c = max(0, min(int(bi), n_shape - 1))
        ei_c = max(0, min(int(ei), n_shape - 1))
        if ei_c < bi_c:
            bi_c, ei_c = ei_c, bi_c
        dist_start = float(gtfs_dist_at_shape[bi_c])
        dist_end = float(gtfs_dist_at_shape[ei_c])
        if dist_end < dist_start:
            dist_start, dist_end = dist_end, dist_start
        names = edge.get("names") or []
        segments.append(
            WaySegment(
                way_id=int(edge["way_id"]),
                dist_start_m=dist_start,
                dist_end_m=dist_end,
                direction=_direction_of(edge),
                name=names[0] if names else None,
                road_class=edge.get("road_class"),
            )
        )

    segments.sort(key=lambda s: s.dist_start_m)

    # Clamp first/last to the GTFS shape bounds (Valhalla's
    # source_percent_along/target_percent_along trim the matched route to the
    # actual entry/exit point on the first/last edge, so the matched shape
    # may not extend all the way to dist=0 or shape_length).
    if segments and shape_length_m > 0:
        first = segments[0]
        if first.dist_start_m > 0:
            segments[0] = WaySegment(
                way_id=first.way_id, dist_start_m=0.0, dist_end_m=first.dist_end_m,
                direction=first.direction, name=first.name, road_class=first.road_class,
            )
        last = segments[-1]
        if last.dist_end_m < shape_length_m:
            segments[-1] = WaySegment(
                way_id=last.way_id, dist_start_m=last.dist_start_m,
                dist_end_m=shape_length_m, direction=last.direction,
                name=last.name, road_class=last.road_class,
            )

    return segments


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def match_shape(
    gtfs_zip_path: str | Path,
    shape_id: str,
    *,
    endpoint: str = "http://localhost:8002",
    costing: str = "bus",
    timeout_s: float = 60.0,
) -> list[WaySegment]:
    """End-to-end map-match for one GTFS shape."""
    polyline, dist = load_gtfs_shape_with_dist(gtfs_zip_path, shape_id)
    if dist is None:
        raise ValueError(
            f"shape {shape_id!r} has no shape_dist_traveled in shapes.txt; "
            "cannot anchor segments to a route ruler"
        )
    response = call_valhalla(polyline, endpoint=endpoint, costing=costing, timeout_s=timeout_s)
    return extract_segments(response, polyline, dist)


def build_cache(
    gtfs_zip_path: str | Path,
    *,
    endpoint: str = "http://localhost:8002",
    shape_ids: Iterable[str] | None = None,
    costing: str = "bus",
    timeout_s: float = 60.0,
    progress: bool = True,
    progress_every: int = 10,
) -> dict[str, list[WaySegment]]:
    """Build the full cache. ``shape_ids=None`` selects all bus-route shapes."""
    targets = list(shape_ids) if shape_ids is not None else list_bus_shapes(gtfs_zip_path)
    cache: dict[str, list[WaySegment]] = {}
    n_ok = n_err = 0
    if progress:
        print(f"[way_match] Building cache for {len(targets)} shape(s) via {endpoint}")
    for i, sid in enumerate(targets, start=1):
        try:
            cache[sid] = match_shape(
                gtfs_zip_path, sid, endpoint=endpoint, costing=costing, timeout_s=timeout_s
            )
            n_ok += 1
        except ValhallaUnreachable:
            raise
        except Exception as e:  # noqa: BLE001
            cache[sid] = []
            n_err += 1
            if progress:
                print(f"[way_match]   {sid}: ERROR {e}")
        if progress and (i % progress_every == 0 or i == len(targets)):
            print(f"[way_match]   [{i}/{len(targets)}] ok={n_ok} err={n_err}")
    return cache


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def save_cache(
    cache: dict[str, list[WaySegment]], out_path: str | Path
) -> Path:
    """Write the cache as plain JSON: ``{shape_id: [{way_id, ...}, ...]}``."""
    payload = {sid: [asdict(seg) for seg in segs] for sid, segs in cache.items()}
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload))
    return out


def load_cache(in_path: str | Path) -> dict[str, list[WaySegment]]:
    payload = json.loads(Path(in_path).read_text())
    return {
        sid: [WaySegment(**d) for d in segs] for sid, segs in payload.items()
    }
