"""Loading raw AVL CSVs and GTFS shapes."""

from __future__ import annotations

import csv
import io as _io
import zipfile
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

DEADHEAD_ROUTE_ID = "992"


def load_avl_csv(path: str | Path, drop_deadhead: bool = True) -> pd.DataFrame:
    """Load a CTA AVL archive CSV.

    Parses ``avl_event_time`` to datetime, drops the ``992`` pseudo-route by
    default, and sorts within each ``trip_id`` by event time.
    """
    df = pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[""])
    df["avl_event_time"] = pd.to_datetime(df["avl_event_time"], format="%Y-%m-%d %H:%M:%S.%f")
    df["latitude"] = df["latitude"].astype(float)
    df["longitude"] = df["longitude"].astype(float)
    if drop_deadhead:
        df = df[df["route_id"] != DEADHEAD_ROUTE_ID].copy()
    df = df.sort_values(["trip_id", "avl_event_time"]).reset_index(drop=True)
    return df


@lru_cache(maxsize=8)
def _read_shapes(gtfs_zip_path: str) -> dict[str, dict]:
    """Cache shapes.txt parse keyed by absolute zip path.

    Returns ``{shape_id: {"polyline": (N, 2) np.ndarray of (lat, lon),
                          "dist_along_m": (N,) np.ndarray or None}}``.
    """
    shapes: dict[str, list[tuple[int, float, float, str]]] = {}
    with zipfile.ZipFile(gtfs_zip_path) as z:
        with z.open("shapes.txt") as f:
            text = _io.TextIOWrapper(f, encoding="utf-8-sig")
            for row in csv.DictReader(text):
                shapes.setdefault(row["shape_id"], []).append(
                    (
                        int(row["shape_pt_sequence"]),
                        float(row["shape_pt_lat"]),
                        float(row["shape_pt_lon"]),
                        row.get("shape_dist_traveled", "") or "",
                    )
                )
    out: dict[str, dict] = {}
    for sid, pts in shapes.items():
        pts.sort(key=lambda x: x[0])
        polyline = np.array([(lat, lon) for _, lat, lon, _ in pts], dtype=float)
        sdt_strs = [s for _, _, _, s in pts]
        if all(s != "" for s in sdt_strs):
            # CTA stores shape_dist_traveled in feet.
            dist_m = np.array([float(s) / 3.28084 for s in sdt_strs], dtype=float)
        else:
            dist_m = None
        out[sid] = {"polyline": polyline, "dist_along_m": dist_m}
    return out


def load_gtfs_shape(gtfs_zip_path: str | Path, shape_id: str) -> np.ndarray:
    """Return the ordered ``(lat, lon)`` polyline for ``shape_id``."""
    shapes = _read_shapes(str(Path(gtfs_zip_path).resolve()))
    if shape_id not in shapes:
        raise KeyError(f"shape_id {shape_id!r} not in {gtfs_zip_path}")
    return shapes[shape_id]["polyline"]


def load_gtfs_shape_with_dist(
    gtfs_zip_path: str | Path, shape_id: str
) -> tuple[np.ndarray, np.ndarray | None]:
    """Return ``(polyline, dist_along_m_per_vertex)``.

    The second element is ``None`` when ``shape_dist_traveled`` isn't
    populated for that shape; otherwise it's an ``(N,)`` array of meters.
    Use this to keep ping projections in the same coordinate system as
    ``stop_times.shape_dist_traveled`` — otherwise stop locations and bus
    distances accumulate independent geodesic errors and disagree.
    """
    shapes = _read_shapes(str(Path(gtfs_zip_path).resolve()))
    if shape_id not in shapes:
        raise KeyError(f"shape_id {shape_id!r} not in {gtfs_zip_path}")
    return shapes[shape_id]["polyline"], shapes[shape_id]["dist_along_m"]


def shape_id_for_pattern(pattern_id: str) -> str:
    """CTA convention: GTFS shape_id = '678' + zero-padded 5-digit pattern_id.

    Four-digit patterns get the familiar '6780' prefix (3936 -> 67803936),
    but five-digit patterns use the fifth digit (29251 -> 67829251), so the
    pad — not a literal '6780' — is the real rule.
    """
    return f"678{int(pattern_id):05d}"


GTFS_ROUTE_TYPE_BUS = "3"


@lru_cache(maxsize=8)
def _read_routes_and_trips(gtfs_zip_path: str) -> tuple[dict[str, str], list[dict]]:
    """Cache routes.txt + trips.txt parses keyed by absolute zip path.

    Returns ``(route_id -> route_type, list-of-trip-rows)``.
    """
    with zipfile.ZipFile(gtfs_zip_path) as z:
        with z.open("routes.txt") as f:
            text = _io.TextIOWrapper(f, encoding="utf-8-sig")
            route_type = {r["route_id"]: r["route_type"] for r in csv.DictReader(text)}
        with z.open("trips.txt") as f:
            text = _io.TextIOWrapper(f, encoding="utf-8-sig")
            trips = list(csv.DictReader(text))
    return route_type, trips


def list_shape_ids(gtfs_zip_path: str | Path, route_type: str | None = None) -> list[str]:
    """Distinct shape_ids referenced by trips on routes of ``route_type``.

    ``route_type`` follows GTFS conventions: ``"3"`` = bus, ``"0"`` = tram,
    ``"1"`` = subway/metro, ``"2"`` = rail, etc. ``None`` = no filter (all
    shape_ids referenced by any trip). The function ignores shape_ids that
    appear in ``shapes.txt`` but are not referenced by any trip.
    """
    route_type_map, trips = _read_routes_and_trips(str(Path(gtfs_zip_path).resolve()))
    out: set[str] = set()
    for t in trips:
        if not t.get("shape_id"):
            continue
        if route_type is not None:
            rt = route_type_map.get(t["route_id"])
            if rt != route_type:
                continue
        out.add(t["shape_id"])
    return sorted(out)


def list_bus_shapes(gtfs_zip_path: str | Path) -> list[str]:
    """Convenience wrapper: distinct shape_ids on bus routes (``route_type=3``)."""
    return list_shape_ids(gtfs_zip_path, route_type=GTFS_ROUTE_TYPE_BUS)


_FT_PER_M = 3.28084


def load_route_stops(gtfs_zip_path: str | Path, shape_id: str) -> list[dict]:
    """Return ordered ``[{name, stop_id, dist_along_m}]`` for stops on ``shape_id``.

    Uses one representative trip with that shape_id and ``stop_times.shape_dist_traveled``
    (CTA stores this in feet). Falls back to ``KeyError`` if no matching trip.
    """
    with zipfile.ZipFile(gtfs_zip_path) as z:
        # 1. find a trip with this shape_id
        with z.open("trips.txt") as f:
            text = _io.TextIOWrapper(f, encoding="utf-8-sig")
            trip_id = None
            for row in csv.DictReader(text):
                if row["shape_id"] == shape_id:
                    trip_id = row["trip_id"]
                    break
        if trip_id is None:
            raise KeyError(f"no trips found with shape_id={shape_id!r}")

        # 2. stop_times for that trip
        with z.open("stop_times.txt") as f:
            text = _io.TextIOWrapper(f, encoding="utf-8-sig")
            stop_rows = [r for r in csv.DictReader(text) if r["trip_id"] == trip_id]
        stop_rows.sort(key=lambda r: int(r["stop_sequence"]))

        # 3. stop names from stops.txt
        with z.open("stops.txt") as f:
            text = _io.TextIOWrapper(f, encoding="utf-8-sig")
            stops_meta = {r["stop_id"]: r for r in csv.DictReader(text)}

    out = []
    for r in stop_rows:
        sid = r["stop_id"]
        meta = stops_meta.get(sid, {})
        dist_ft = float(r["shape_dist_traveled"]) if r.get("shape_dist_traveled") else None
        if dist_ft is None:
            continue
        out.append(
            {
                "stop_id": sid,
                "name": meta.get("stop_name", sid),
                "dist_along_m": dist_ft / _FT_PER_M,
            }
        )
    return out
