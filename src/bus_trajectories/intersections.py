"""Enrich a way-match cache with the controlled features along a route.

For each shape in a way-match cache, we identify two kinds of nodes that
constrain the bus's speed:

1. **Controlled intersections** — nodes where the bus's OSM way meets another
   road, and the bus's direction of travel is controlled:

   - The intersection node carries ``highway=traffic_signals`` (signal
     applies to all approaches by default).
   - A node on the bus's way, just upstream of the intersection in the
     bus's traversal direction, carries ``highway=stop`` or
     ``highway=give_way`` with a ``direction`` tag that matches the bus's
     traversal.

2. **Pedestrian crossings** — ``highway=crossing`` nodes on the bus's way
   that are either signalised (``crossing=traffic_signals`` and related
   ``crossing_ref`` values) or marked (``crossing=marked`` / ``zebra`` /
   ``uncontrolled``). Unmarked crossings are skipped since they rarely
   cause buses to actually stop. Crossings co-located with a captured
   intersection (within ~40 m) are dropped to avoid double-counting the
   same physical delay.

Intersections where the control is on a side street the bus doesn't use are
treated as "bus has free right-of-way" and skipped.

The output anchors each ControlPoint to the route's ``shape_dist_traveled``
ruler — the same coordinate system the trajectory pipeline uses, so a
ControlPoint at ``dist_along_route_m=4032`` is at the same position as a
smoothed trajectory point at the same distance value.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

import numpy as np

from .io import load_gtfs_shape_with_dist
from .mapmatch.shape_snap import SnapToShapeMatcher
from .way_match import WaySegment, load_cache as load_way_cache

# How far back from an intersection (along the bus's way) to look for a
# stop or yield sign. Stop signs are conventionally placed within ~10 m of
# the intersection, but we use 30 m for tolerance — OSM tagging is sometimes
# imprecise.
STOP_SIGN_PROXIMITY_M = 30.0

# Maximum perpendicular distance from a node to the GTFS shape for the node
# to count as "on the route". 30 m is generous enough to handle GPS noise
# and shape sampling differences without picking up off-route nodes.
DEFAULT_PERP_THRESHOLD_M = 30.0

# Default cluster threshold for merging adjacent same-control-type
# intersections that look like one physical intersection in OSM. Consecutive
# ControlPoints within this distance get merged. 0.015 mi ≈ 24 m. Tight
# enough that only signal nodes that are essentially co-located (e.g., two
# OSM nodes at one physical signalized junction) get folded together.
DEFAULT_CLUSTER_GAP_M = 0.015 * 1609.344  # ~24.1 m

# Default control types to keep in the output. give_way is dropped by
# default since OSM tagging of yields on bus arterials is sparse and noisy.
DEFAULT_KEEP_TYPES: tuple[str, ...] = (
    "traffic_signals",
    "stop",
    "ped_crossing_signal",
    "ped_crossing_marked",
)

# Pedestrian crossings within this along-route distance of an intersection
# vertex are anchored to that vertex (their delay can be associated with the
# physical intersection; multiple crossings at the same intersection share
# an anchor id). Crossings beyond this radius from any vertex are mid-block.
# 40 m ≈ half a typical Chicago block. This is no longer a merge threshold —
# every crossing is still emitted as its own ControlPoint; the radius just
# decides whether an anchor id gets recorded on it.
DEFAULT_ANCHOR_RADIUS_M = 40.0

DEFAULT_OVERPASS_ENDPOINT = "https://overpass-api.de/api/interpreter"


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ControlPoint:
    """One controlled feature along a route.

    For traffic_signals / stop / give_way, the ``intersection_node_id`` is
    the topological intersection vertex on the bus's way (the node OSM
    shares between the bus's road and a cross-street).

    For ped_crossing_signal / ped_crossing_marked the ``intersection_node_id``
    is the crossing's own OSM node id; the *intersection* it belongs to
    (if any) is recorded separately in ``anchor_intersection_node_id``.
    """

    intersection_node_id: int
    lat: float
    lon: float
    dist_along_route_m: float
    on_way_id: int
    control_type: str  # "traffic_signals" | "stop" | "give_way" | "ped_crossing_signal" | "ped_crossing_marked"
    cross_street_names: tuple[str, ...]
    # When ControlPoints get merged (e.g., two signal nodes that are
    # really one physical intersection split across two OSM ways), this
    # tracks the OSM node_ids that were folded into this representative.
    merged_node_ids: tuple[int, ...] = ()

    # Pedestrian-crossing metadata. Populated only for ped_crossing_*
    # ControlPoints; default-empty for everything else.
    #
    # ``anchor_intersection_node_id`` — the intersection vertex on the bus's
    # way that this crossing belongs to, or ``None`` for a true mid-block
    # crossing with no intersection vertex nearby. Multiple crossings at
    # the same physical intersection share the same anchor id.
    #
    # ``signalized`` — derived from ``control_type`` for ped_crossing_*
    # but stored explicitly so downstream code can filter on a single
    # boolean without parsing the type string.
    #
    # ``markings`` — the value of OSM ``crossing:markings`` (e.g. "zebra",
    # "lines", "dashes", "ladder", "yes"). Empty string if absent.
    #
    # ``has_island`` — true iff OSM ``crossing:island=yes`` (a pedestrian
    # refuge in the middle of the crossing).
    anchor_intersection_node_id: int | None = None
    signalized: bool = False
    markings: str = ""
    has_island: bool = False


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class OverpassError(RuntimeError):
    """Generic Overpass error (HTTP non-2xx, malformed JSON, error response)."""


class OverpassUnreachable(OverpassError):
    """Overpass endpoint not reachable (connection refused, DNS, TLS, timeout)."""


# ---------------------------------------------------------------------------
# Overpass client
# ---------------------------------------------------------------------------


def query_overpass(
    way_ids: Iterable[int],
    *,
    endpoint: str = DEFAULT_OVERPASS_ENDPOINT,
    timeout_s: float = 120.0,
) -> dict:
    """Fetch the given OSM ways with their geometries + tags, plus all
    referenced node coordinates and tags.

    Single POST, JSON response. Following the ``way_match.call_valhalla``
    HTTP pattern: stdlib urllib, User-Agent header, named exceptions on
    failure.
    """
    ids = sorted(int(w) for w in way_ids)
    if not ids:
        return {"elements": []}

    # Overpass QL: we need the bus's ways AND every way that crosses one of
    # them (so cross-street names + node membership are visible). Using
    # explicit named sets is more reliable than relying on the implicit
    # default-set behavior of `>;` / `<;`.
    #
    #   .bus_ways  = the bus's ways (selected by id)
    #   .bus_nodes = every node of those ways
    #   .all_ways  = every way that includes any of those nodes (= bus ways
    #                + cross streets at every intersection along the route)
    #   .all_nodes = every node of every all_way (so we have lat/lon and
    #                tags for nodes on cross-street approaches too)
    ids_str = ",".join(str(i) for i in ids)
    query = (
        f"[out:json][timeout:{int(timeout_s)}];\n"
        f"way(id:{ids_str}) -> .bus_ways;\n"
        f"node(w.bus_ways) -> .bus_nodes;\n"
        f"way(bn.bus_nodes) -> .all_ways;\n"
        f"node(w.all_ways) -> .all_nodes;\n"
        f"(.all_ways; .all_nodes;);\n"
        f"out body;\n"
    )
    data = urllib.parse.urlencode({"data": query}).encode("utf-8")
    req = urllib.request.Request(
        endpoint,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "bus-trajectories/intersections (research)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            payload = resp.read()
    except urllib.error.HTTPError as e:
        body_text = ""
        try:
            body_text = e.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        raise OverpassError(
            f"Overpass HTTP {e.code} {e.reason}: {body_text[:500]}"
        ) from e
    except urllib.error.URLError as e:
        raise OverpassUnreachable(
            f"could not reach Overpass at {endpoint}: {e.reason}"
        ) from e

    try:
        return json.loads(payload)
    except json.JSONDecodeError as e:
        raise OverpassError(f"Overpass returned non-JSON ({e})") from e


# ---------------------------------------------------------------------------
# Core algorithm
# ---------------------------------------------------------------------------


def _split_osm_elements(osm: dict) -> tuple[dict[int, dict], dict[int, dict]]:
    """Return ``(ways_by_id, nodes_by_id)`` from an Overpass response.

    Each ways_by_id[id] = {"id": int, "nodes": [node_id, ...], "tags": {...}}
    Each nodes_by_id[id] = {"id": int, "lat": float, "lon": float, "tags": {...}}
    """
    ways: dict[int, dict] = {}
    nodes: dict[int, dict] = {}
    for el in osm.get("elements", []):
        t = el.get("type")
        if t == "way":
            ways[int(el["id"])] = {
                "id": int(el["id"]),
                "nodes": [int(n) for n in el.get("nodes", [])],
                "tags": el.get("tags") or {},
            }
        elif t == "node":
            nodes[int(el["id"])] = {
                "id": int(el["id"]),
                "lat": float(el["lat"]),
                "lon": float(el["lon"]),
                "tags": el.get("tags") or {},
            }
    return ways, nodes


# OSM ``highway=*`` values that count as vehicle roads for intersection
# purposes. A pedestrian footway sharing a node with the bus's road is a
# crosswalk, not a cross-street — it must not turn the crossing node into
# an "intersection vertex".
_VEHICLE_HIGHWAY_TYPES: frozenset[str] = frozenset((
    "motorway", "trunk", "primary", "secondary", "tertiary",
    "unclassified", "residential", "service", "living_street",
    "motorway_link", "trunk_link", "primary_link", "secondary_link",
    "tertiary_link", "road", "busway",
))


def _node_to_highway_ways(ways_by_id: dict[int, dict]) -> dict[int, set[int]]:
    """Index: node_id → set of way_ids that include it AND are vehicle roads.

    Only ways whose ``highway`` tag is in :data:`_VEHICLE_HIGHWAY_TYPES` are
    indexed. Footways, cycleways, pedestrian paths, etc. are excluded so
    that a ``highway=crossing`` node — which is typically also a vertex of
    a footway-as-crosswalk way — is not mistaken for an intersection
    vertex.
    """
    out: dict[int, set[int]] = {}
    for w in ways_by_id.values():
        h = (w["tags"] or {}).get("highway", "").lower()
        if h not in _VEHICLE_HIGHWAY_TYPES:
            continue
        for nid in w["nodes"]:
            out.setdefault(nid, set()).add(w["id"])
    return out


def _pedestrian_crossing_type(tags: dict) -> str | None:
    """Classify a ``highway=crossing`` node by its variant.

    Returns:
        ``"ped_crossing_signal"`` — signalised pedestrian crossing (Pelican,
        Puffin, Toucan, etc.), where the bus must wait on a red.
        ``"ped_crossing_marked"`` — marked but un-signalised crossing
        (zebra, marked, "uncontrolled"). Buses must yield to pedestrians.
        ``None`` — unmarked / unknown / not a crossing. Skipped.
    """
    if (tags.get("highway") or "").lower() != "crossing":
        return None
    crossing = (tags.get("crossing") or "").lower()
    cref = (tags.get("crossing_ref") or "").lower()
    if crossing == "traffic_signals" or cref in (
        "pelican", "puffin", "toucan", "pegasus", "signals"
    ):
        return "ped_crossing_signal"
    # "uncontrolled" in OSM legacy tagging means "marked, no signal" — buses
    # still stop. New OSM guidance is to use crossing=marked; we accept both.
    if crossing in ("marked", "zebra", "uncontrolled") or cref == "zebra":
        return "ped_crossing_marked"
    # Newer schema: crossing:markings=yes/zebra/lines/...
    if (tags.get("crossing:markings") or "").lower() in (
        "yes", "zebra", "lines", "dashes", "ladder"
    ):
        return "ped_crossing_marked"
    return None


def _direction_applies(direction_tag: str | None, bus_direction: str) -> bool:
    """Does an OSM direction tag apply to a bus traversing in ``bus_direction``?

    Bus direction is one of "forward" / "reverse" / "unknown" (from
    WaySegment). OSM ``direction`` is one of "forward" / "backward" /
    "both" / absent.
    """
    if direction_tag in (None, "", "both"):
        return True
    if bus_direction == "unknown":
        # Be conservative: if we don't know which way the bus traverses,
        # assume the directional sign applies.
        return True
    if direction_tag == "forward":
        return bus_direction == "forward"
    if direction_tag == "backward":
        return bus_direction == "reverse"
    # Unknown direction value — treat as applying (conservative).
    return True


def cluster_signals(
    cps: list[ControlPoint],
    *,
    max_gap_m: float = DEFAULT_CLUSTER_GAP_M,
) -> list[ControlPoint]:
    """Merge clusters of same-type ControlPoints that are part of one physical
    intersection.

    Two consecutive ControlPoints are merged when:
      - they have the same ``control_type``, AND
      - their route-distance gap is ≤ ``max_gap_m``.

    The cluster's representative is the FIRST ControlPoint in route order
    (so its ``intersection_node_id`` and lat/lon stay stable). The merged
    representative's ``cross_street_names`` is the union of cluster
    members'; ``merged_node_ids`` records every node folded in.
    """
    if not cps:
        return []
    cps_sorted = sorted(cps, key=lambda c: c.dist_along_route_m)
    out: list[ControlPoint] = [cps_sorted[0]]
    cluster_names: set[str] = set(out[0].cross_street_names)
    cluster_merged: list[int] = []

    for cp in cps_sorted[1:]:
        prev = out[-1]
        gap = cp.dist_along_route_m - prev.dist_along_route_m
        same_type = cp.control_type == prev.control_type

        if same_type and gap <= max_gap_m:
            cluster_names |= set(cp.cross_street_names)
            cluster_merged.append(cp.intersection_node_id)
            out[-1] = ControlPoint(
                intersection_node_id=prev.intersection_node_id,
                lat=prev.lat,
                lon=prev.lon,
                dist_along_route_m=prev.dist_along_route_m,
                on_way_id=prev.on_way_id,
                control_type=prev.control_type,
                cross_street_names=tuple(sorted(cluster_names)),
                merged_node_ids=tuple(cluster_merged),
            )
        else:
            out.append(cp)
            cluster_names = set(cp.cross_street_names)
            cluster_merged = []
    return out


def find_intersections_for_shape(
    way_cache: list[WaySegment],
    polyline_latlon: np.ndarray,
    dist_along_m_per_vertex: np.ndarray,
    osm_data: dict,
    *,
    perp_threshold_m: float = DEFAULT_PERP_THRESHOLD_M,
    stop_sign_proximity_m: float = STOP_SIGN_PROXIMITY_M,
    keep_types: tuple[str, ...] = DEFAULT_KEEP_TYPES,
    cluster_gap_m: float = DEFAULT_CLUSTER_GAP_M,
    anchor_radius_m: float = DEFAULT_ANCHOR_RADIUS_M,
) -> list[ControlPoint]:
    """For one shape's way-cache, return every controlled feature on the route.

    Two passes:

    1. Walk the bus's ways in route order, collect every "intersection
       vertex" (= node shared between the bus's way and at least one
       non-bus highway way) regardless of whether it is controlled. While
       walking, track stop/yield signs to attribute each one to the next
       intersection vertex downstream on the bus's approach. Adjacent
       same-type intersection vertices that share at least one non-bus
       way are merged topologically (handles the OSM-way-splitting case
       where a single physical intersection emits two endpoint vertices).

    2. Walk every ``highway=crossing`` node on the bus's ways. For each,
       record its OSM metadata (markings, island, signalised flag) and
       anchor it to the nearest intersection vertex within
       ``anchor_radius_m`` along the route, or ``None`` if mid-block.
       Crossings are NOT merged — every one is emitted.

    Output is sorted by ``dist_along_route_m`` and filtered by
    ``keep_types``.
    """
    ways_by_id, nodes_by_id = _split_osm_elements(osm_data)
    node_to_ways = _node_to_highway_ways(ways_by_id)

    matcher = SnapToShapeMatcher(
        polyline_latlon,
        max_perp_m=max(perp_threshold_m * 5, 200.0),
        dist_along_m_per_vertex=dist_along_m_per_vertex,
    )

    proj_cache: dict[int, tuple[float, float] | None] = {}

    def project(node_id: int) -> tuple[float, float] | None:
        if node_id in proj_cache:
            return proj_cache[node_id]
        n = nodes_by_id.get(node_id)
        if n is None:
            proj_cache[node_id] = None
            return None
        res = matcher.match(np.array([n["lat"]]), np.array([n["lon"]]))
        result = (float(res.dist_along_m[0]), float(res.perp_dist_m[0]))
        proj_cache[node_id] = result
        return result

    bus_way_ids = {seg.way_id for seg in way_cache}

    # ── Pass 1: collect intersection vertices in route order ──────────────
    intersection_vertices: list[dict] = []
    seen_intersection_nodes: set[int] = set()

    for seg in way_cache:
        way = ways_by_id.get(seg.way_id)
        if way is None:
            continue
        nodes_in_order = (
            way["nodes"] if seg.direction != "reverse"
            else list(reversed(way["nodes"]))
        )
        pending_stop: tuple[int, float, str] | None = None

        for node_id in nodes_in_order:
            node = nodes_by_id.get(node_id)
            if node is None:
                continue
            tags = node.get("tags") or {}
            proj = project(node_id)
            if proj is None:
                continue
            dist_route_m, perp_m = proj
            if perp_m > perp_threshold_m:
                continue
            if not (seg.dist_start_m - 1e-3 <= dist_route_m <= seg.dist_end_m + 1e-3):
                continue

            highway_tag = tags.get("highway")

            # Track stop/give_way signs on the bus's approach.
            if highway_tag in ("stop", "give_way") and _direction_applies(
                tags.get("direction"), seg.direction
            ):
                pending_stop = (node_id, dist_route_m, highway_tag)

            # Intersection vertices only: must share a node with at least
            # one non-bus highway way.
            other_ways = node_to_ways.get(node_id, set()) - bus_way_ids
            if not other_ways:
                continue
            if node_id in seen_intersection_nodes:
                continue
            seen_intersection_nodes.add(node_id)

            # Classify the vertex.
            control_type: str | None = None
            if highway_tag == "traffic_signals":
                control_type = "traffic_signals"
            elif (
                pending_stop is not None
                and (dist_route_m - pending_stop[1]) <= stop_sign_proximity_m
                and (dist_route_m - pending_stop[1]) >= -1e-3
            ):
                control_type = pending_stop[2]

            cross_names: list[str] = []
            for wid in sorted(other_ways):
                w = ways_by_id.get(wid)
                if w is None:
                    continue
                name = (w["tags"] or {}).get("name")
                if name and name not in cross_names:
                    cross_names.append(name)

            intersection_vertices.append({
                "node_id": int(node_id),
                "lat": float(node["lat"]),
                "lon": float(node["lon"]),
                "dist_along_route_m": dist_route_m,
                "on_way_id": int(seg.way_id),
                "control_type": control_type,  # None = uncontrolled
                "cross_way_ids": frozenset(int(w) for w in other_ways),
                "cross_street_names": tuple(cross_names),
                "merged_node_ids": (),
            })

            if control_type in ("stop", "give_way"):
                pending_stop = None

    # ── Pass 1b: topological merge — adjacent same-control-type signal
    # vertices that share at least one non-bus way are the same physical
    # intersection split across two OSM way endpoints. Fold the second into
    # the first. (Y-junctions where the cross-streets differ legitimately
    # remain distinct: they share no non-bus way.)
    intersection_vertices.sort(key=lambda v: v["dist_along_route_m"])
    merged_vertices: list[dict] = []
    i = 0
    while i < len(intersection_vertices):
        head = dict(intersection_vertices[i])
        merged_ids = list(head["merged_node_ids"])
        cross_names_set = set(head["cross_street_names"])
        cumulative_cross_ways = set(head["cross_way_ids"])
        j = i + 1
        while (
            head["control_type"] == "traffic_signals"
            and j < len(intersection_vertices)
            and intersection_vertices[j]["control_type"] == "traffic_signals"
            and cumulative_cross_ways & intersection_vertices[j]["cross_way_ids"]
        ):
            v_next = intersection_vertices[j]
            merged_ids.append(v_next["node_id"])
            cumulative_cross_ways |= set(v_next["cross_way_ids"])
            cross_names_set.update(v_next["cross_street_names"])
            j += 1
        head["merged_node_ids"] = tuple(merged_ids)
        head["cross_street_names"] = tuple(sorted(cross_names_set))
        head["cross_way_ids"] = frozenset(cumulative_cross_ways)
        merged_vertices.append(head)
        i = j
    intersection_vertices = merged_vertices

    # ── Pass 2: ped crossings, every one emitted, each anchored to the
    # nearest intersection vertex within anchor_radius_m (controlled or
    # uncontrolled — anchoring uses all vertices we found in pass 1).
    import bisect
    vertex_dists = [v["dist_along_route_m"] for v in intersection_vertices]

    ped_controls: list[ControlPoint] = []
    seen_ped_nodes: set[int] = set()
    for seg in way_cache:
        way = ways_by_id.get(seg.way_id)
        if way is None:
            continue
        for node_id in way["nodes"]:
            if node_id in seen_ped_nodes:
                continue
            node = nodes_by_id.get(node_id)
            if node is None:
                continue
            tags = node.get("tags") or {}
            ped_type = _pedestrian_crossing_type(tags)
            if ped_type is None:
                continue
            proj = project(node_id)
            if proj is None:
                continue
            dist_route_m, perp_m = proj
            if perp_m > perp_threshold_m:
                continue
            if not (seg.dist_start_m - 1e-3 <= dist_route_m <= seg.dist_end_m + 1e-3):
                continue
            seen_ped_nodes.add(node_id)

            # Nearest intersection vertex within anchor_radius_m
            anchor_node_id: int | None = None
            if vertex_dists:
                idx = bisect.bisect_left(vertex_dists, dist_route_m)
                best_d, best_i = float("inf"), None
                for ci in (idx - 1, idx):
                    if 0 <= ci < len(vertex_dists):
                        d_to = abs(vertex_dists[ci] - dist_route_m)
                        if d_to < best_d:
                            best_d, best_i = d_to, ci
                if best_i is not None and best_d <= anchor_radius_m:
                    anchor_node_id = intersection_vertices[best_i]["node_id"]

            markings = (tags.get("crossing:markings") or "").strip().lower()
            if not markings:
                # Legacy: ``crossing=zebra|marked|uncontrolled`` implies
                # markings exist even when crossing:markings is absent.
                c_val = (tags.get("crossing") or "").lower()
                if c_val in ("zebra", "marked", "uncontrolled"):
                    markings = "yes"
                elif (tags.get("crossing_ref") or "").lower() == "zebra":
                    markings = "zebra"
            has_island = (tags.get("crossing:island") or "").lower() == "yes"
            signalized = ped_type == "ped_crossing_signal"

            ped_controls.append(ControlPoint(
                intersection_node_id=int(node_id),
                lat=float(node["lat"]),
                lon=float(node["lon"]),
                dist_along_route_m=dist_route_m,
                on_way_id=int(seg.way_id),
                control_type=ped_type,
                cross_street_names=(),
                merged_node_ids=(),
                anchor_intersection_node_id=anchor_node_id,
                signalized=signalized,
                markings=markings,
                has_island=has_island,
            ))

    # ── Emit: controlled intersection vertices + every ped crossing.
    out: list[ControlPoint] = []
    for v in intersection_vertices:
        if v["control_type"] is None:
            continue  # uncontrolled vertex — used for anchoring only
        out.append(ControlPoint(
            intersection_node_id=v["node_id"],
            lat=v["lat"],
            lon=v["lon"],
            dist_along_route_m=v["dist_along_route_m"],
            on_way_id=v["on_way_id"],
            control_type=v["control_type"],
            cross_street_names=v["cross_street_names"],
            merged_node_ids=v["merged_node_ids"],
            signalized=v["control_type"] == "traffic_signals",
        ))
    out.extend(ped_controls)

    out = [c for c in out if c.control_type in keep_types]
    out.sort(key=lambda c: c.dist_along_route_m)

    # Clusters: signals are already topologically merged in pass 1b; ped
    # crossings are never merged; only stop/give_way still get the
    # proximity-based clustering (preserves the previous behaviour for
    # those rarer cases of two stop signs that are really one).
    if cluster_gap_m > 0:
        clusterable = ("stop", "give_way")
        to_cluster = [c for c in out if c.control_type in clusterable]
        others = [c for c in out if c.control_type not in clusterable]
        if to_cluster:
            to_cluster = cluster_signals(to_cluster, max_gap_m=cluster_gap_m)
        out = sorted(others + to_cluster, key=lambda c: c.dist_along_route_m)
    return out


# ---------------------------------------------------------------------------
# Top-level
# ---------------------------------------------------------------------------


def build_intersections(
    way_cache_path: str | Path,
    gtfs_zip_path: str | Path,
    *,
    shape_ids: Iterable[str] | None = None,
    overpass_endpoint: str = DEFAULT_OVERPASS_ENDPOINT,
    perp_threshold_m: float = DEFAULT_PERP_THRESHOLD_M,
    stop_sign_proximity_m: float = STOP_SIGN_PROXIMITY_M,
    keep_types: tuple[str, ...] = DEFAULT_KEEP_TYPES,
    cluster_gap_m: float = DEFAULT_CLUSTER_GAP_M,
    anchor_radius_m: float = DEFAULT_ANCHOR_RADIUS_M,
    progress: bool = True,
) -> dict[str, list[ControlPoint]]:
    """Build ``{shape_id: [ControlPoint, ...]}`` from a way-match cache."""
    way_cache = load_way_cache(way_cache_path)
    targets = list(shape_ids) if shape_ids is not None else list(way_cache.keys())
    missing = [s for s in targets if s not in way_cache]
    if missing:
        raise KeyError(f"shape(s) not in way-cache: {missing}")

    # One Overpass query for all way_ids in scope.
    all_way_ids: set[int] = set()
    for sid in targets:
        all_way_ids.update(seg.way_id for seg in way_cache[sid])
    if progress:
        print(f"[intersections] Overpass: fetching {len(all_way_ids)} ways…")
    osm = query_overpass(all_way_ids, endpoint=overpass_endpoint)
    if progress:
        n_elem = len(osm.get("elements") or [])
        print(f"[intersections] Overpass: {n_elem} elements")

    out: dict[str, list[ControlPoint]] = {}
    for sid in targets:
        polyline, dist = load_gtfs_shape_with_dist(gtfs_zip_path, sid)
        cps = find_intersections_for_shape(
            way_cache[sid],
            polyline,
            dist,
            osm,
            perp_threshold_m=perp_threshold_m,
            stop_sign_proximity_m=stop_sign_proximity_m,
            keep_types=keep_types,
            cluster_gap_m=cluster_gap_m,
            anchor_radius_m=anchor_radius_m,
        )
        out[sid] = cps
        if progress:
            counts: dict[str, int] = {}
            for c in cps:
                counts[c.control_type] = counts.get(c.control_type, 0) + 1
            summary = ", ".join(f"{n} {t}" for t, n in sorted(counts.items())) or "-"
            print(f"[intersections] shape {sid}: {len(cps)} intersections ({summary})")
    return out


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def save_intersections(
    data: dict[str, list[ControlPoint]], out_path: str | Path
) -> Path:
    """JSON: ``{shape_id: [{...}, ...]}``."""
    payload = {sid: [asdict(c) for c in cps] for sid, cps in data.items()}
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload))
    return out


def load_intersections(in_path: str | Path) -> dict[str, list[ControlPoint]]:
    payload = json.loads(Path(in_path).read_text())
    out: dict[str, list[ControlPoint]] = {}
    for sid, cps in payload.items():
        loaded: list[ControlPoint] = []
        for d in cps:
            anchor = d.get("anchor_intersection_node_id")
            loaded.append(ControlPoint(
                intersection_node_id=int(d["intersection_node_id"]),
                lat=float(d["lat"]),
                lon=float(d["lon"]),
                dist_along_route_m=float(d["dist_along_route_m"]),
                on_way_id=int(d["on_way_id"]),
                control_type=str(d["control_type"]),
                cross_street_names=tuple(d.get("cross_street_names") or ()),
                merged_node_ids=tuple(int(n) for n in (d.get("merged_node_ids") or ())),
                anchor_intersection_node_id=(int(anchor) if anchor is not None else None),
                signalized=bool(d.get("signalized", False)),
                markings=str(d.get("markings") or ""),
                has_island=bool(d.get("has_island", False)),
            ))
        out[sid] = loaded
    return out
