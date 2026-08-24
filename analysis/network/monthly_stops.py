"""Per-MONTH registered stop locations from door events.

Stops physically move over 2.5 years (bay changes, construction, stop
consolidation), so a single registration from the 2026 window would
mis-place door events in 2024. This re-registers every month.

The historical bus-state export carries no stop_id, so the AVL stamp that
the 2026-only registration grouped on is unavailable. Instead:

  1. assign each door event to the nearest stop ON ITS TRIP'S OWN PATTERN
     (nearest along the pattern shape). The pattern comes from joining the
     event's (bus_id, trip_id) to the AVL archive, whose shape is the era
     GTFS shape with the matching id suffix. Globally-nearest-pole grouping
     was wrong at Loop corners (2026-08-24, stop 18126): a stop's berth can
     sit closer to the CROSS street's pole than its own — 70% of 18126's
     service cluster fell nearest to Dearborn's pole 61 m away — and it
     could land events on the opposite-direction stop across the street.
     Pattern restriction excludes both by construction. Events whose trip
     has no archive pattern (~7%) are dropped from registration;
  2. per stop, take the modal 10 ft cell of its door events and that cell's
     centre of mass — the registered location for that month;
  3. project the registered point onto each era shape that serves it to get
     ``off_m`` in the segment's downstream-signal frame, and classify
     near/far side against the bounding signals.

Output: outputs/network/<city>/monthly_stops/<YYYYMM>.json
    {seg_id: [{id, name, off_m, signal_side, signal_dist_ft, n_door}]}
consumed by delay_events for location-based door re-attribution.

Usage:
    PYTHONPATH=src uv run python analysis/network/monthly_stops.py \
        --city cta --months 2024-01:2026-08
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import time
import zipfile
from collections import defaultdict
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from analysis.network import gtfs_history  # noqa: E402
from dataio.cities import get_city  # noqa: E402
from dataio.gtfs import load_gtfs_shape_with_dist  # noqa: E402

POLE_MAX_M = 60.0     # door event -> candidate stop pole
CELL_FT = 10.0        # modal cell size, matching the 10 ft distribution grid
MIN_EVENTS = 5        # stops with fewer door events keep the GTFS pole
SNAP_MAX_M = 40.0     # registered point -> shape
FT_PER_M = 3.28084
SIDE_WINDOW_FT = 150.0


def _stop_poles(gtfs_zip: Path) -> dict[str, dict]:
    with zipfile.ZipFile(gtfs_zip) as z, z.open("stops.txt") as f:
        return {
            r["stop_id"]: {"name": r.get("stop_name", ""),
                           "lat": float(r["stop_lat"]),
                           "lon": float(r["stop_lon"])}
            for r in csv.DictReader(io.TextIOWrapper(f, "utf-8-sig"))
            if r.get("stop_lat") and r.get("stop_lon")
        }


def _shape_stops(gtfs_zip: Path, shape_ids: set[str]) -> dict[str, list[str]]:
    """shape_id -> ordered stop_ids, via one representative trip per shape."""
    with zipfile.ZipFile(gtfs_zip) as z:
        rep: dict[str, str] = {}
        with z.open("trips.txt") as f:
            for r in csv.DictReader(io.TextIOWrapper(f, "utf-8-sig")):
                sid = (r.get("shape_id") or "").strip()
                if sid in shape_ids and sid not in rep:
                    rep[sid] = r["trip_id"]
        want = {t: s for s, t in rep.items()}
        seq: dict[str, list] = defaultdict(list)
        with z.open("stop_times.txt") as f:
            for r in csv.DictReader(io.TextIOWrapper(f, "utf-8-sig")):
                s = want.get(r["trip_id"])
                if s:
                    seq[s].append((int(r["stop_sequence"]), r["stop_id"]))
    return {s: [x[1] for x in sorted(v)] for s, v in seq.items()}


def _door_points(city, month: str, shape_of_pattern: dict) -> "pd.DataFrame":
    """Active door cycles for one month, each labelled with its trip's era
    SHAPE via the (bus_id, trip_id) -> pattern join against the AVL archive.
    ~93% of trips resolve; the rest return shape_id None."""
    import duckdb
    import pandas as pd  # noqa: F401

    f = Path(city.door_source_dir) / f"month={month}.parquet"
    if not f.exists() or not city.avl_source_dir:
        import pandas as pd
        return pd.DataFrame()
    arch = f"{city.avl_source_dir}/date={month[:4]}-{month[4:]}-*.parquet"
    con = duckdb.connect()
    df = con.execute(f"""
        WITH pat AS (
          SELECT bus_id, trip_id,
                 -- one pattern per bus-trip; mode() breaks the rare ties
                 mode(pattern_id) AS pattern_id
          FROM read_parquet('{arch}')
          WHERE trip_id IS NOT NULL AND pattern_id IS NOT NULL
          GROUP BY 1, 2)
        SELECT d.latitude, d.longitude, pat.pattern_id
        FROM read_parquet('{f}') d
        LEFT JOIN pat ON pat.bus_id = d.bus_id AND pat.trip_id = d.trip_id
        WHERE d.latitude IS NOT NULL AND d.longitude IS NOT NULL
          AND coalesce(d.ron,0)+coalesce(d.roff,0)
              +coalesce(d.fon,0)+coalesce(d.foff,0) > 0
    """).fetch_df()
    df["shape_id"] = df["pattern_id"].map(
        lambda v: shape_of_pattern.get(int(v)) if v == v and v is not None
        else None)
    return df


def build_month(city, month: str, canon: dict, out_dir: Path) -> dict:
    from scipy.spatial import cKDTree

    mid = f"{month[:4]}-{month[4:]}-15"
    era = gtfs_history.era_id(city, mid)
    zpath = gtfs_history.era_zip(city, mid)
    if zpath is None or not Path(zpath).exists():
        print(f"  {month}: no GTFS era, skipping")
        return {}
    era_shapes_p = (REPO / "outputs" / "network" / city.city_id
                    / "era_shapes" / f"{era}.json")
    if not era_shapes_p.exists():
        print(f"  {month}: era_shapes/{era}.json missing, skipping")
        return {}
    shapes = json.loads(era_shapes_p.read_text())
    poles = _stop_poles(Path(zpath))
    # era shape per PATTERN: CTA shape ids are <3-digit era prefix><pattern>
    shape_of_pattern: dict[int, str] = {}
    for sid in shapes:
        try:
            shape_of_pattern[int(sid[3:])] = sid
        except ValueError:
            continue
    pts = _door_points(city, month, shape_of_pattern)
    if pts is None or not len(pts) or not poles:
        print(f"  {month}: no door points / poles")
        return {}
    n_all = len(pts)
    pts = pts[pts.shape_id.notna()].reset_index(drop=True)

    sstops = _shape_stops(Path(zpath), set(shapes))
    mlat = 111320.0 * np.cos(np.radians(float(pts.latitude.mean())))

    # Per shape: snap its trips' door events onto the polyline, attribute each
    # to the nearest of THAT PATTERN's stops (along-shape distance — the same
    # rule delay_events uses for re-attribution), then pool per stop_id.
    cell = CELL_FT / FT_PER_M
    ev_xy: dict[str, list] = defaultdict(list)
    for sid, grp in pts.groupby("shape_id"):
        stop_ids = [st for st in sstops.get(sid, []) if st in poles]
        if not stop_ids:
            continue
        try:
            poly, dist = load_gtfs_shape_with_dist(Path(zpath), sid)
        except Exception:
            continue
        poly = np.asarray(poly, float)
        cum = (np.asarray(dist, float) if dist is not None else None)
        if cum is None or len(poly) < 2:
            continue
        sxy = np.column_stack([poly[:, 1] * mlat, poly[:, 0] * 111320.0])
        seg = np.hypot(*np.diff(sxy, axis=0).T)
        dp, dd = [], []
        for i in range(len(sxy) - 1):
            n = max(1, int(seg[i] // 5.0))
            t = np.linspace(0, 1, n, endpoint=False)
            dp.append(sxy[i] + t[:, None] * (sxy[i + 1] - sxy[i]))
            dd.append(cum[i] + t * (cum[i + 1] - cum[i]))
        dp.append(sxy[-1:]); dd.append(cum[-1:])
        dp = np.concatenate(dp); dd = np.concatenate(dd)
        tree = cKDTree(dp)
        # candidate positions: this pattern's poles projected on this shape
        cand = np.array([[poles[st]["lon"] * mlat, poles[st]["lat"] * 111320.0]
                         for st in stop_ids])
        cd, ci = tree.query(cand, distance_upper_bound=SNAP_MAX_M * 2)
        cok = np.isfinite(cd)
        c_along = dd[np.clip(ci, 0, len(dd) - 1)][cok]
        c_ids = np.array(stop_ids, dtype=object)[cok]
        srt = np.argsort(c_along)
        c_along, c_ids = c_along[srt], c_ids[srt]
        if not len(c_ids):
            continue
        exy = np.column_stack([grp.longitude.to_numpy() * mlat,
                               grp.latitude.to_numpy() * 111320.0])
        ed, ei = tree.query(exy, distance_upper_bound=POLE_MAX_M)
        eok = np.isfinite(ed)
        e_along = dd[np.clip(ei, 0, len(dd) - 1)][eok]
        exy = exy[eok]
        idx = np.searchsorted(c_along, e_along)
        lo = np.clip(idx - 1, 0, len(c_along) - 1)
        hi = np.clip(idx, 0, len(c_along) - 1)
        pick = np.where(np.abs(e_along - c_along[lo])
                        <= np.abs(e_along - c_along[hi]), lo, hi)
        for k, st in enumerate(c_ids[pick]):
            ev_xy[st].append(exy[k])

    reg: dict[str, tuple[float, float, int]] = {}
    for st, rows in ev_xy.items():
        if len(rows) < MIN_EVENTS:
            continue
        blk = np.asarray(rows)
        keys = np.floor(blk / cell).astype(np.int64)
        uniq, inv, cnt = np.unique(keys, axis=0, return_inverse=True,
                                   return_counts=True)
        top = int(np.argmax(cnt))
        sel = blk[inv == top]
        cx, cy = sel.mean(axis=0)
        reg[st] = (cy / 111320.0, cx / mlat, len(rows))
    print(f"    {month}: {n_all:,} events, {len(pts):,} with pattern "
          f"({len(pts)/max(n_all,1):.0%}), {len(reg):,} stops registered",
          flush=True)

    # project registered points onto each era shape -> segment offsets
    per_seg: dict[str, dict] = defaultdict(dict)
    for sid, rec in shapes.items():
        stop_ids = sstops.get(sid) or []
        if not stop_ids:
            continue
        bounds = rec["seg_bounds"]
        if not bounds:
            continue
        try:
            poly, dist = load_gtfs_shape_with_dist(Path(zpath), sid)
        except Exception:
            continue
        poly = np.asarray(poly, float)
        cum = (np.asarray(dist, float) if dist is not None
               else np.r_[0, np.cumsum(np.hypot(
                   np.diff(poly[:, 0]) * 111320,
                   np.diff(poly[:, 1]) * mlat))])
        sxy = np.column_stack([poly[:, 1] * mlat, poly[:, 0] * 111320.0])
        seg = np.hypot(*np.diff(sxy, axis=0).T)
        dp, dd = [], []
        for i in range(len(sxy) - 1):
            n = max(1, int(seg[i] // 5.0))
            t = np.linspace(0, 1, n, endpoint=False)
            dp.append(sxy[i] + t[:, None] * (sxy[i + 1] - sxy[i]))
            dd.append(cum[i] + t * (cum[i + 1] - cum[i]))
        dp.append(sxy[-1:]); dd.append(cum[-1:])
        dp = np.concatenate(dp); dd = np.concatenate(dd)
        tree = cKDTree(dp)
        lo = np.array([b[1] for b in bounds])
        hi = np.array([b[2] for b in bounds])
        for st in stop_ids:
            src = reg.get(st)
            if src is None:
                p = poles.get(st)
                if p is None:
                    continue
                src = (p["lat"], p["lon"], 0)
            q = np.array([src[1] * mlat, src[0] * 111320.0])
            dist_perp, idx = tree.query(q, distance_upper_bound=SNAP_MAX_M)
            if not np.isfinite(dist_perp):
                continue
            x = float(dd[idx])
            k = int(np.searchsorted(hi, x))
            if k >= len(bounds) or not (lo[k] <= x <= hi[k]):
                continue
            seg_id, x0, x1 = bounds[k]
            off_m = round(x1 - x, 1)
            L_ft = (x1 - x0) * FT_PER_M
            d_near = off_m * FT_PER_M
            d_far = L_ft - d_near
            if 0 <= d_near <= SIDE_WINDOW_FT and (
                    not (0 <= d_far <= SIDE_WINDOW_FT) or d_near <= d_far):
                side, sd = "near_side", round(d_near, 1)
            elif 0 <= d_far <= SIDE_WINDOW_FT:
                side, sd = "far_side", -round(d_far, 1)
            else:
                side, sd = "other", None
            prev = per_seg[seg_id].get(st)
            if prev is None or src[2] > prev.get("n_door", 0):
                per_seg[seg_id][st] = {
                    "id": st,
                    "name": poles.get(st, {}).get("name", ""),
                    "off_m": off_m,
                    "signal_side": side,
                    "signal_dist_ft": sd,
                    "n_door": src[2],
                }
    out = {s: sorted(v.values(), key=lambda r: -r["off_m"])
           for s, v in per_seg.items()}
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{month}.json").write_text(json.dumps(out))
    n_stops = sum(len(v) for v in out.values())
    n_reg = sum(1 for v in out.values() for s in v if s["n_door"] >= MIN_EVENTS)
    print(f"  {month}: era {era}, {len(pts):,} door pts, {len(out):,} segments, "
          f"{n_stops:,} stop instances ({n_reg:,} door-registered)", flush=True)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--city", default="cta")
    ap.add_argument("--months", default="2024-01:2026-08",
                    help="YYYY-MM:YYYY-MM inclusive")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    city = get_city(a.city)
    reg = json.loads((REPO / "outputs" / "network" / city.city_id
                      / "segment_registry.json").read_text())
    out_dir = REPO / "outputs" / "network" / city.city_id / "monthly_stops"
    s, e = a.months.split(":")
    y, m = int(s[:4]), int(s[5:7])
    ey, em = int(e[:4]), int(e[5:7])
    t0 = time.time()
    while (y, m) <= (ey, em):
        month = f"{y:04d}{m:02d}"
        p = out_dir / f"{month}.json"
        if not (p.exists() and p.stat().st_size > 0 and not a.force):
            build_month(city, month, reg, out_dir)
        m += 1
        if m > 12:
            y, m = y + 1, 1
    print(f"done ({time.time() - t0:.0f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
