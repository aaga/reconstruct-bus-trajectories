"""Signals OSM misses: infer from signalized crosswalks, plus exceptions.

Runs after way_match (which writes ``city.intersections_file``) and before
the registry, rewriting the intersections file in place:

1. Crosswalk inference (cities with ``infer_signals_from_crossings``). An
   uncontrolled junction on a route becomes ``traffic_signals`` when OSM
   has >= 2 signalized crosswalks within CROSS_RADIUS_M of it on different
   legs (bearings >= LEG_SEP_DEG apart) and no ``highway=traffic_signals``
   node within SIGNAL_EXCL_M. Some mappers tag signalized crosswalks around
   a junction but never the junction itself (RIPTA, 2026-11-03: Main St &
   Randall St in Pawtucket left a 2.6 km segment; 18 of 19 candidates
   confirmed signalized on street view, the miss is a signal_reject).
2. ``signal_add`` exceptions promote listed junction nodes (OSM has no
   evidence at all); ``signal_reject`` exceptions block inference and
   demote OSM signals at listed nodes (dataio/exceptions.json).

Promoted junctions are clustered among themselves with the same 30 m rule
as OSM signals, and signalized ped crossings within 40 m of them are
dropped, as find_intersections_for_shape does for OSM signals. Idempotent:
only uncontrolled junctions are promoted, so a rerun changes nothing.

    PYTHONPATH=src uv run python analysis/network/signal_infer.py --city ripta
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import math
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from dataio.cities import get_city  # noqa: E402
from dataio.exceptions import load_exceptions  # noqa: E402
from dataio.intersections import (  # noqa: E402
    DEFAULT_CLUSTER_GAP_M,
    _cluster_split_sides,
    _filter_near,
    cluster_signals,
    load_intersections,
    save_intersections,
)

CROSS_RADIUS_M = 25.0
SIGNAL_EXCL_M = 40.0
LEG_SEP_DEG = 60.0
ON_NODE_M = 3.0  # a crosswalk this close to the junction is its own leg
_CELL = 0.0005   # ~50 m grid for neighbour lookups


def _dist_m(lat1, lon1, lat2, lon2) -> float:
    return math.hypot((lon2 - lon1) * 111320.0 * math.cos(math.radians(lat1)),
                      (lat2 - lat1) * 110540.0)


def _bearing(lat1, lon1, lat2, lon2) -> float:
    return math.degrees(math.atan2(
        (lon2 - lon1) * math.cos(math.radians(lat1)), lat2 - lat1)) % 360


class _Grid:
    def __init__(self, pts):
        self.cells: dict[tuple[int, int], list] = {}
        for p in pts:
            self.cells.setdefault(self._key(p[0], p[1]), []).append(p)

    @staticmethod
    def _key(lat, lon):
        return (int(math.floor(lat / _CELL)), int(math.floor(lon / _CELL)))

    def near(self, lat, lon, r_m):
        k = self._key(lat, lon)
        span = int(math.ceil(r_m / 40.0))
        for di in range(-span, span + 1):
            for dj in range(-span, span + 1):
                for p in self.cells.get((k[0] + di, k[1] + dj), ()):
                    if _dist_m(lat, lon, p[0], p[1]) <= r_m:
                        yield p


def is_signalized_crossing(tags: dict) -> bool:
    if tags.get("highway") != "crossing":
        return False
    return (tags.get("crossing") == "traffic_signals"
            or tags.get("crossing:signals") == "yes"
            or (tags.get("crossing_ref") or "").lower()
            in ("pelican", "puffin", "toucan", "pegasus", "signals"))


def crossing_rule(lat, lon, xing_grid: _Grid, sig_grid: _Grid) -> int:
    """Number of legs with a signalized crosswalk (0 when the rule fails)."""
    if any(True for _ in sig_grid.near(lat, lon, SIGNAL_EXCL_M)):
        return 0
    xs = list(xing_grid.near(lat, lon, CROSS_RADIUS_M))
    if len(xs) < 2:
        return 0
    legs: list[float | None] = []
    for x in xs:
        if _dist_m(lat, lon, x[0], x[1]) <= ON_NODE_M:
            if None not in legs:
                legs.append(None)
            continue
        b = _bearing(lat, lon, x[0], x[1])
        if all(c is None or min(abs(b - c), 360 - abs(b - c)) >= LEG_SEP_DEG
               for c in legs):
            legs.append(b)
    return len(legs) if len(legs) >= 2 else 0


def apply(cps_by_shape: dict, *, xings, signals, infer: bool,
          adds: frozenset[int], rejects: frozenset[int]):
    """Return (new cps_by_shape, report rows)."""
    xg, sg = _Grid(xings), _Grid(signals)
    split_sides = _cluster_split_sides()
    report: dict[int, dict] = {}
    out = {}
    for sid, cps in cps_by_shape.items():
        promoted, rest = [], []
        for c in cps:
            nodes = {c.intersection_node_id, *c.merged_node_ids}
            if c.control_type == "traffic_signals" and nodes & rejects:
                rest.append(dataclasses.replace(
                    c, control_type="uncontrolled_junction", signalized=False))
                report.setdefault(c.intersection_node_id, dict(
                    node=c.intersection_node_id, lat=c.lat, lon=c.lon,
                    cross=list(c.cross_street_names), source="signal_reject"))
                continue
            if c.control_type != "uncontrolled_junction" or nodes & rejects:
                rest.append(c)
                continue
            src = None
            if nodes & adds:
                src = "signal_add"
            elif infer:
                n_legs = crossing_rule(c.lat, c.lon, xg, sg)
                if n_legs:
                    src = f"crosswalks:{n_legs}_legs"
            if src is None:
                rest.append(c)
                continue
            promoted.append(dataclasses.replace(
                c, control_type="traffic_signals", signalized=True))
            report.setdefault(c.intersection_node_id, dict(
                node=c.intersection_node_id, lat=c.lat, lon=c.lon,
                cross=list(c.cross_street_names), source=src))
        if promoted:
            promoted = cluster_signals(promoted, max_gap_m=DEFAULT_CLUSTER_GAP_M,
                                       split_sides=split_sides)
        merged = sorted(rest + promoted, key=lambda c: c.dist_along_route_m)
        if promoted:
            merged = _filter_near(merged, drop_type="ped_crossing_signal",
                                  near_types=("traffic_signals",),
                                  max_dist_m=SIGNAL_EXCL_M)
        out[sid] = merged
    return out, sorted(report.values(), key=lambda r: -r["lat"])


def read_pbf_points(pbf: Path):
    import osmium

    xings, signals = [], []
    for n in osmium.FileProcessor(str(pbf), osmium.osm.NODE).with_filter(
            osmium.filter.KeyFilter("highway")):
        tags = dict(n.tags)
        if tags.get("highway") == "traffic_signals":
            signals.append((n.location.lat, n.location.lon))
        elif is_signalized_crossing(tags):
            xings.append((n.location.lat, n.location.lon))
    return xings, signals


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--city", required=True)
    args = ap.parse_args()
    city = get_city(args.city)
    path = city.resolve(city.intersections_file)
    exc = load_exceptions(city.city_id)
    infer = city.infer_signals_from_crossings
    if not (infer or exc.signal_adds or exc.signal_rejects):
        print(f"signal_infer[{city.city_id}]: nothing to do")
        return
    if infer and not city.pbf_file:
        raise SystemExit("infer_signals_from_crossings needs city.pbf_file")
    xings, signals = (read_pbf_points(city.resolve(city.pbf_file))
                      if city.pbf_file else ([], []))
    cps = load_intersections(path)
    before = sum(c.control_type == "traffic_signals" for v in cps.values() for c in v)
    new, report = apply(cps, xings=xings, signals=signals, infer=infer,
                        adds=exc.signal_adds, rejects=exc.signal_rejects)
    after = sum(c.control_type == "traffic_signals" for v in new.values() for c in v)
    save_intersections(new, path)
    rep_path = path.with_name("signal_infer_report.json")
    if report:  # a rerun on already-promoted input changes nothing; keep the last report
        rep_path.write_text(json.dumps(report, indent=1))
    print(f"signal_infer[{city.city_id}]: {len(xings)} signalized crosswalks, "
          f"{len(signals)} OSM signals; {len(report)} junction nodes changed; "
          f"signal CPs {before} -> {after} across shapes -> {rep_path}")


if __name__ == "__main__":
    main()
