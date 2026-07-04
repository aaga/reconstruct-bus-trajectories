"""Build the three stop/shape bundles the phone app ships, per city:

  data/<city>_bus_stops.json     [{stpid, name, lat, lon}] — every bus stop,
                                 for the offline nearby-stop search.
  data/<city>_pattern_stops.json {pattern_id: [{stpid, name, lat, lon,
                                 dist_along_m, is_near_side}]} — ordered stops
                                 per pattern. dist_along_m is the stop's
                                 distance projected onto the pattern shape (so
                                 it shares a basis with <city>_pattern_shapes).
                                 is_near_side is null where it can't be
                                 classified (all of MTA today).
  data/<city>_pattern_shapes.json {pattern_id: [[lon, lat, cumDist_m], ...]} —
                                 the route polyline, used to place the phone's
                                 GPS fix along the route and pick the next stop.

Pattern identity is per-city:
  CTA — the BusTime pattern id, recovered from the "678"+pid shape_id.
  MTA — "<route_id>_<direction_id>", matching providers/mta.js.

Near-side classification (CTA only) reuses
dataio.intersections.classify_near_side_stops against
caches/cta/intersections.json (built by build_all_intersections.py), with
intersections_route22.json as a fallback. MTA ships is_near_side: null.

Usage:
    python observation_tool/scripts/build_stops.py [--city cta|mta]
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import zipfile
from pathlib import Path

from gtfs_util import OBS_TOOL, REPO, ensure_city_gtfs  # noqa: E402 (sets sys.path)

from dataio.intersections import (  # noqa: E402
    classify_near_side_stops,
    load_intersections,
)
from dataio.gtfs import list_bus_shapes  # noqa: E402

DATA_DIR = OBS_TOOL / "data"
FT_PER_M = 3.28084
# CTA stop_id convention: 1-29999 bus stops, 30000+ rail stations/platforms.
MAX_BUS_STOP_ID = 30000

INTERSECTION_SOURCES = [
    REPO / "caches" / "cta" / "intersections.json",
    REPO / "intersections_route22.json",
]


# ----------------------------------------------------------- geometry helpers

def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p = math.pi / 180
    dlat = (lat2 - lat1) * p
    dlon = (lon2 - lon1) * p
    a = (math.sin(dlat / 2) ** 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin(dlon / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


# Geometry fidelity for the shipped polyline. The phone only needs to know which
# stop is next (27 m / 90 ft buffer), so simplifying raw GTFS shapes (a point
# every few metres) to ~8 m loses no useful precision and shrinks the bundle ~5x.
SIMPLIFY_EPS_M = 8.0


def _rdp_keep(pts_m, eps):
    """Ramer–Douglas–Peucker on planar metres; returns a keep-mask."""
    n = len(pts_m)
    keep = [False] * n
    keep[0] = keep[-1] = True
    stack = [(0, n - 1)]
    while stack:
        s, e = stack.pop()
        ax, ay = pts_m[s]
        bx, by = pts_m[e]
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        maxd, idx = -1.0, -1
        for i in range(s + 1, e):
            px, py = pts_m[i]
            if seg2 == 0:
                d = math.hypot(px - ax, py - ay)
            else:
                t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / seg2))
                d = math.hypot(px - (ax + t * dx), py - (ay + t * dy))
            if d > maxd:
                maxd, idx = d, i
        if maxd > eps and idx != -1:
            keep[idx] = True
            stack.append((s, idx))
            stack.append((idx, e))
    return keep


def shape_polyline(points):
    """points: [(seq, lat, lon)] -> simplified [[lon, lat, cumDist_m], ...]."""
    pts = [(lat, lon) for _, lat, lon in sorted(points)]
    if len(pts) >= 3:
        mx = math.cos(pts[0][0] * math.pi / 180) * 111320
        my = 110540
        proj = [(lon * mx, lat * my) for lat, lon in pts]
        keep = _rdp_keep(proj, SIMPLIFY_EPS_M)
        pts = [p for p, k in zip(pts, keep) if k]
    out = []
    cum = 0.0
    prev = None
    for lat, lon in pts:
        if prev is not None:
            cum += haversine_m(prev[0], prev[1], lat, lon)
        out.append([round(lon, 6), round(lat, 6), round(cum, 1)])
        prev = (lat, lon)
    return out


def project_dist(lat, lon, poly):
    """Along-route metres of the nearest point on `poly` to (lat, lon).

    Mirrors geo.js:projectOntoShape — local equirectangular projection."""
    if not poly or len(poly) < 2:
        return None
    mx = math.cos(lat * math.pi / 180) * 111320
    my = 110540
    px, py = lon * mx, lat * my
    best = None
    for i in range(len(poly) - 1):
        a, b = poly[i], poly[i + 1]
        ax, ay = a[0] * mx, a[1] * my
        bx, by = b[0] * mx, b[1] * my
        dx, dy = bx - ax, by - ay
        seg2 = dx * dx + dy * dy
        t = ((px - ax) * dx + (py - ay) * dy) / seg2 if seg2 else 0.0
        t = max(0.0, min(1.0, t))
        cx, cy = ax + t * dx, ay + t * dy
        off2 = (px - cx) ** 2 + (py - cy) ** 2
        if best is None or off2 < best[0]:
            seg_m = math.sqrt(seg2)
            best = (off2, a[2] + t * seg_m)
    return round(best[1], 1) if best else None


# ----------------------------------------------------------------- gtfs io

def open_csv(z: zipfile.ZipFile, name: str) -> csv.DictReader:
    return csv.DictReader(io.TextIOWrapper(z.open(name), encoding="utf-8-sig"))


def cta_is_bus_stop(row: dict) -> bool:
    if row.get("location_type") not in ("", "0", None):
        return False
    try:
        return int(row["stop_id"]) < MAX_BUS_STOP_ID
    except ValueError:
        return False


def mta_is_bus_stop(row: dict) -> bool:
    return row.get("location_type") in ("", "0", None)


def cta_pattern_key(route_id, direction_id, shape_id):
    # CTA: shape_id = "678" + zero-padded 5-digit pattern id; BusTime reports
    # the pattern id without leading zeros.
    if not shape_id.startswith("678") or len(shape_id) != 8:
        return None
    return str(int(shape_id[3:]))


def mta_pattern_key(route_id, direction_id, shape_id):
    if not route_id:
        return None
    return f"{route_id}_{direction_id or ''}"


CITY = {
    "cta": {"is_bus_stop": cta_is_bus_stop, "pattern_key": cta_pattern_key, "near_side": True},
    "mta": {"is_bus_stop": mta_is_bus_stop, "pattern_key": mta_pattern_key, "near_side": False},
}


# --------------------------------------------------------------------- build

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--city", choices=sorted(CITY), default="cta")
    args = ap.parse_args()
    city = args.city
    cfg = CITY[city]

    zips = ensure_city_gtfs(city)
    DATA_DIR.mkdir(exist_ok=True)

    # Bus shapes: CTA filters rail out by route_type; MTA bus feeds are bus-only.
    bus_shapes: set[str] | None = None
    if city == "cta":
        bus_shapes = set()
        for zp in zips:
            bus_shapes |= set(list_bus_shapes(zp))
        print(f"[stops] {len(bus_shapes)} bus shapes")

    stops_meta: dict[str, dict] = {}
    bus_stops: dict[str, dict] = {}            # stop_id -> entry (dedupe across feeds)
    rep: dict[str, dict] = {}                  # pattern_key -> {trip_id, shape_id}
    trip_to_key: dict[str, str] = {}           # rep trip_id -> pattern_key
    want_shapes: set[str] = set()

    # ---- stops.txt + trips.txt across every feed.
    for zp in zips:
        with zipfile.ZipFile(zp) as z:
            for row in open_csv(z, "stops.txt"):
                stops_meta[row["stop_id"]] = row
                if cfg["is_bus_stop"](row) and row["stop_id"] not in bus_stops:
                    bus_stops[row["stop_id"]] = {
                        "stpid": row["stop_id"],
                        "name": row["stop_name"],
                        "lat": round(float(row["stop_lat"]), 6),
                        "lon": round(float(row["stop_lon"]), 6),
                    }
            for row in open_csv(z, "trips.txt"):
                sid = row.get("shape_id") or ""
                if bus_shapes is not None and sid not in bus_shapes:
                    continue
                key = cfg["pattern_key"](row.get("route_id", ""),
                                         row.get("direction_id", ""), sid)
                if key is None or key in rep:
                    continue
                rep[key] = {"trip_id": row["trip_id"], "shape_id": sid}
                trip_to_key[row["trip_id"]] = key
                if sid:
                    want_shapes.add(sid)

    out1 = DATA_DIR / f"{city}_bus_stops.json"
    out1.write_text(json.dumps(list(bus_stops.values()), separators=(",", ":")))
    print(f"[stops] {out1.name}: {len(bus_stops)} stops "
          f"({out1.stat().st_size / 1e6:.1f} MB)")
    print(f"[stops] representative trips for {len(rep)} patterns")

    # ---- stop_times.txt: rows for rep trips only. (seq, stop_id, sdt_metres)
    stop_rows: dict[str, list] = {k: [] for k in rep}
    for zp in zips:
        with zipfile.ZipFile(zp) as z:
            for row in open_csv(z, "stop_times.txt"):
                key = trip_to_key.get(row["trip_id"])
                if key is None:
                    continue
                sdt = row.get("shape_dist_traveled") or ""
                sdt_m = float(sdt) / FT_PER_M if sdt else None
                stop_rows[key].append((int(row["stop_sequence"]), row["stop_id"], sdt_m))

    # ---- shapes.txt: polylines for the rep shapes.
    shape_pts: dict[str, list] = {sid: [] for sid in want_shapes}
    for zp in zips:
        with zipfile.ZipFile(zp) as z:
            if "shapes.txt" not in z.namelist():
                continue
            for row in open_csv(z, "shapes.txt"):
                sid = row.get("shape_id")
                if sid in shape_pts:
                    shape_pts[sid].append((
                        int(row["shape_pt_sequence"]),
                        float(row["shape_pt_lat"]),
                        float(row["shape_pt_lon"]),
                    ))
    polylines = {sid: shape_polyline(p) for sid, p in shape_pts.items() if p}

    # ---- intersections (CTA near-side only).
    control_points: dict[str, list] = {}
    if cfg["near_side"]:
        for src in INTERSECTION_SOURCES:
            if not src.exists():
                print(f"[stops] note: {src.name} not found — skipping")
                continue
            loaded = load_intersections(src)
            for sid, cps in loaded.items():
                control_points.setdefault(sid, cps)
            print(f"[stops] {src.name}: control points for {len(loaded)} shapes")

    # ---- assemble per-pattern stop lists + shapes.
    pattern_stops: dict[str, list] = {}
    pattern_shapes: dict[str, list] = {}
    n_flagged = n_unknown = 0
    for key, info in rep.items():
        rows = sorted(stop_rows.get(key, []))
        if not rows:
            continue
        sid = info["shape_id"]
        poly = polylines.get(sid)
        if poly:
            pattern_shapes[key] = poly

        # Near-side (CTA): classify against the shape's control points using the
        # shape_dist_traveled basis the intersections were computed in.
        near_side = None
        if cfg["near_side"]:
            cps = control_points.get(sid)
            if cps:
                ns_stops = [{"stop_id": stpid, "dist_along_m": sdt}
                            for _, stpid, sdt in rows if sdt is not None]
                near_side = classify_near_side_stops(ns_stops, cps)
        if near_side is None:
            n_unknown += 1

        entry = []
        for _, stpid, sdt in rows:
            meta = stops_meta.get(stpid, {})
            lat = round(float(meta["stop_lat"]), 6) if meta.get("stop_lat") else None
            lon = round(float(meta["stop_lon"]), 6) if meta.get("stop_lon") else None
            if poly and lat is not None:
                dist = project_dist(lat, lon, poly)
            else:
                dist = round(sdt, 1) if sdt is not None else None
            flag = None if near_side is None else (stpid in near_side)
            if flag:
                n_flagged += 1
            entry.append({
                "stpid": stpid,
                "name": meta.get("stop_name", stpid),
                "lat": lat,
                "lon": lon,
                "dist_along_m": dist,
                "is_near_side": flag,
            })
        pattern_stops[key] = entry

    out2 = DATA_DIR / f"{city}_pattern_stops.json"
    out2.write_text(json.dumps(pattern_stops, separators=(",", ":")))
    print(f"[stops] {out2.name}: {len(pattern_stops)} patterns, {n_flagged} near-side "
          f"flags, {n_unknown} without near-side ({out2.stat().st_size / 1e6:.1f} MB)")

    out3 = DATA_DIR / f"{city}_pattern_shapes.json"
    out3.write_text(json.dumps(pattern_shapes, separators=(",", ":")))
    print(f"[stops] {out3.name}: {len(pattern_shapes)} shapes "
          f"({out3.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
