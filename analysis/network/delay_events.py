"""Network-wide delay-EVENT extraction (2026-07 feature).

Re-reconstructs every trip (same gates/assignment as run_reconstruct) and
detects discrete slowdown events (speed < 5 mph sustained >= 15 s, the
corridor-study detector). Each event is classified against the vehicle's
door-open intervals — VERIFIED empirically: door ``event_time`` is the OPEN
instant and ``dwell_s`` runs to close, so the interval is
``[event_time, event_time + dwell_s]``:

    no temporal overlap          -> class "nd"  (non-dwell delay event)
    overlap, >10 s before open   -> class "pre" (pre-boarding dwell portion)
    overlap, >10 s after close   -> class "post"(post-boarding dwell portion)

Dwell (2026-07-29 decision): EVERY door cycle contributes — dwell seconds =
union(door interval ∪ any overlapping slow events), merged across touching
cycles, positioned via the trajectory at the blob's time midpoint. Quick
stops that never trigger a 15 s event still count. Pax-weighted delay =
nd events + ONLY the >10 s pre/post shoulders (the viz pieces), each piece
weighted by the load as-of the most recent door close before it.

Each classified row gets a LOCATION expressed as meters upstream of the
segment's DOWNSTREAM signal (queues at the light cluster near 0):
  * dw rows (2026-08-05): the door cycle's RAW reported lat/lon snapped to
    the assigned shape (trajectory-at-close fallback when it won't snap) —
    immune to the union-midpoint smearing that mislocated far-side stops.
    Blobs are cut at segment-boundary crossings; each door-bearing slice
    is a dw row in its door's raw segment. Door-less slices aren't dwell.
  * pre/post/post2: trajectory midpoint of the portion, but only keep the
    boarding class when that segment matches the door's raw segment —
    otherwise the piece is a plain nd event, detached from the dwell.
  * nd: trajectory midpoint of the event.

Outputs per (service_date, route), both keyed by CANONICAL seg_id:
  events/…/route=R.parquet     one row per classified event/portion
  event_sums/…/route=R.parquet per-traversal sums powering the redefined
                               metrics: nd_event_s (sum of non-overlapping
                               event seconds), dwell_union_s (sum of
                               event∪door union seconds), pax_event_s
                               (nd event seconds × load carried at event
                               start). NOTE: nd + dwell no longer equals
                               overall delay by construction.

Usage:
    PYTHONPATH=src uv run python analysis/network/delay_events.py --city cta \
        [--date YYYY-MM-DD] [--workers 8] [--force]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from analysis.network.assign_shapes import Assignment, choose_shape, monotone_frac  # noqa: E402
from analysis.network.run_reconstruct import (  # noqa: E402
    _G,
    _init_worker,
    _matcher,
    _service_date_pings,
    MAX_DURATION_H,
    MIN_MONOTONE,
    MIN_PINGS,
    TERMINAL_M,
)
from core.decompose.events import AbsoluteSpeedThreshold, detect_events  # noqa: E402
from core.smooth import locreg_pchip  # noqa: E402
from dataio.cities import CityConfig, get_city  # noqa: E402

THRESHOLD = AbsoluteSpeedThreshold(5.0)
MIN_EVENT_S = 15.0
PORTION_MIN_S = 10.0  # pre/post-boarding portions must exceed this
DENSE_DT_S = 2.0
MAX_LOAD = 150  # APC glitch clip (matches build_payloads)

EVENTS_SCHEMA = pa.schema(
    [
        ("seg_id", pa.dictionary(pa.int32(), pa.string())),
        ("route_id", pa.dictionary(pa.int32(), pa.string())),
        ("service_date", pa.date32()),
        ("trip_key", pa.string()),
        # nd | pre | post (single door cycle) | post2 (>=1 swallowed extra
        # cycle) | dw (dwell blob — metric/annotation row, hidden from bars)
        ("cls", pa.dictionary(pa.int32(), pa.string())),
        ("off_down_m", pa.float32()),  # midpoint, meters upstream of downstream signal
        # Piece-START position on the same axis (may exceed the seg length
        # when the piece began upstream of the boundary). Currently unused
        # downstream; kept so partial regens stay schema-compatible.
        ("off_start_m", pa.float32()),
        ("dur_s", pa.float32()),
        ("hour_local", pa.uint8()),
        ("is_last", pa.bool_()),  # queue marker: traversal's last piece in seg
        # (2026-08-05) generic trip-sequencing: position of this piece within
        # its trip, ordered by t_start_s across ALL pieces (nd/pre/post/post2/
        # dw, all segments). trip_seq±1 on the same trip = the piece before/
        # after. NB: associate on (trip_key, route_id, trip_seq) — CTA
        # trip_ids repeat across routes, so trip_key alone collides for a
        # handful of trips per day (~6/14k on 2026-07-15).
        ("trip_seq", pa.int16()),
        # Like is_last but door events (dw) compete too: the traversal's
        # final piece in the segment including dwells.
        ("is_last_all", pa.bool_()),
        # System stop attribution of the associated door cycle (dw/pre/
        # post/post2 rows; null for nd). Powers per-stop performance stats.
        ("stop_id", pa.string()),
        # Piece time bounds (epoch s), for trip-sequencing analyses.
        ("t_start_s", pa.float64()),
        ("t_end_s", pa.float64()),
    ]
)

SUMS_SCHEMA = pa.schema(
    [
        ("seg_id", pa.dictionary(pa.int32(), pa.string())),
        ("trip_key", pa.string()),
        ("shape_id", pa.dictionary(pa.int32(), pa.string())),
        ("nd_event_s", pa.float32()),
        ("dwell_union_s", pa.float32()),
        ("pax_event_s", pa.float32()),
    ]
)


def _door_intervals(
    city: CityConfig, date_iso: str
) -> tuple[dict[str, np.ndarray], dict[str, list]]:
    """(vehicle -> array[[t_open, t_close, load, lat, lon], ...],
        vehicle -> [stop_id, ...] in the same order).

    lat/lon are the RAW reported door coordinates (2026-08-05: verified to
    be genuine finely-gridded measurements, not stop lookups)."""
    import duckdb

    ev_glob = str(city.resolve("caches/door_events") / city.city_id / "*.parquet")
    cut = city.service_day_cutover_h
    con = duckdb.connect()
    rows = con.execute(
        f"""
        SELECT bus_id,
               epoch((event_time AT TIME ZONE '{city.tz}')) AS t_open,
               dwell_s, passenger_load, latitude, longitude, stop_id
        FROM read_parquet('{ev_glob}')
        WHERE (event_time - INTERVAL {cut} HOUR)::DATE = DATE '{date_iso}'
          -- 2026-07-31 decision: zero-activity door cycles (nobody on or
          -- off) are ignored EVERYWHERE — treated as if the doors never
          -- opened. ~10% of CTA cycles.
          AND coalesce(ron,0) + coalesce(roff,0)
              + coalesce(fon,0) + coalesce(foff,0) > 0
        ORDER BY bus_id, t_open
        """
    ).fetchall()
    out: dict[str, list] = defaultdict(list)
    stops: dict[str, list] = defaultdict(list)
    for bus, t_open, dwell, load, lat, lon, stop_id in rows:
        out[str(bus)].append((float(t_open), float(t_open) + float(dwell or 0.0),
                              min(int(load or 0), MAX_LOAD),
                              float(lat or 0.0), float(lon or 0.0)))
        stops[str(bus)].append(stop_id)
    return ({k: np.asarray(v) for k, v in out.items()}, dict(stops))


def _stored_assignments(city: CityConfig, date_iso: str) -> dict[str, str]:
    """trip_key -> shape_id from the traversal batch's output for this date.

    Exact reuse: choose_shape is deterministic on identical pings, so the
    stored winner IS what re-scoring every candidate would pick — but this
    skips the full candidate scan (the dominant cost of the batch per the
    2026-07 py-spy profile). Trips absent from the lookup (rejected there,
    or a date the traversal batch hasn't covered) fall back to choose_shape.
    """
    import glob as _globmod

    pat = str(
        REPO / "outputs" / "network" / city.city_id / "traversals"
        / f"service_date={date_iso}" / "route=*.parquet"
    )
    if not _globmod.glob(pat):
        return {}
    import duckdb

    con = duckdb.connect()
    rows = con.execute(
        f"SELECT DISTINCT trip_key, shape_id FROM read_parquet('{pat}')"
    ).fetchall()
    return {str(k): str(s) for k, s in rows}


def _process_trip(trip: pd.DataFrame, date_iso: str, doors: dict, rejects: Counter,
                  assigned: dict[str, str] | None = None,
                  door_stops: dict[str, list] | None = None):
    """Returns (event_rows, sum_rows) or None."""
    city: CityConfig = _G["city"]
    trip = trip.sort_values("ts_utc").drop_duplicates(subset="ts_utc")
    if len(trip) < MIN_PINGS:
        rejects["few_pings"] += 1
        return None
    t0 = trip["ts_utc"].iloc[0]
    t0_epoch = t0.timestamp()
    t_sec_all = (trip["ts_utc"] - t0).dt.total_seconds().to_numpy()
    if t_sec_all[-1] > MAX_DURATION_H * 3600:
        rejects["too_long"] += 1
        return None

    route_id = str(trip["route_id"].iloc[0])
    candidates = _G["shapes_by_route"].get(route_id, [])
    if not candidates:
        rejects["route_not_in_gtfs"] += 1
        return None
    lats = trip["latitude"].to_numpy(dtype=float)
    lons = trip["longitude"].to_numpy(dtype=float)
    stored_key = (
        f"{trip['trip_id'].iloc[0]}_{trip['vehicle_id'].iloc[0]}_{date_iso}"
    )
    stored_sid = (assigned or {}).get(stored_key)
    if stored_sid is not None and stored_sid in candidates:
        # Fast path: reuse the traversal batch's winning shape — one match
        # against the winner instead of scoring every candidate. Only
        # on-route rows are consumed downstream, so skip exact far values.
        matcher, shape_len = _matcher(stored_sid)
        asg = Assignment(shape_id=stored_sid, score=1.0, frac_on=1.0,
                         frac_monotone=1.0,
                         match=matcher.match(lats, lons, exact_far=False))
    else:
        matchers = {sid: _matcher(sid)[0] for sid in candidates}
        lens = {sid: _matcher(sid)[1] for sid in candidates}
        got = choose_shape(lats, lons, matchers, lens)
        if isinstance(got, str):
            rejects[got] += 1
            return None
        asg = got
        shape_len = lens[asg.shape_id]

    on = asg.match.on_route
    t_on = t_sec_all[on]
    d_on = asg.match.dist_along_m[on]
    if monotone_frac(d_on) < MIN_MONOTONE:
        rejects["not_monotone"] += 1
        return None
    at_term = np.nonzero(d_on >= shape_len - TERMINAL_M)[0]
    if len(at_term):
        cut = at_term[0] + 1
        t_on, d_on = t_on[:cut], d_on[:cut]
        if len(t_on) < MIN_PINGS:
            rejects["few_pings_after_truncate"] += 1
            return None
    try:
        sm = locreg_pchip(t_on, d_on, bandwidth=city.bandwidth)
    except Exception:
        rejects["smooth_failed"] += 1
        return None
    f = sm.f

    # Dense grid: positions + speeds.
    tg = np.arange(float(f.x[0]), float(f.x[-1]), DENSE_DT_S)
    if len(tg) < 4:
        rejects["too_short"] += 1
        return None
    xg = np.asarray(f(tg))
    xg = np.maximum.accumulate(xg)
    vg = np.gradient(xg, tg) * 2.23694  # mph

    events = detect_events(tg, xg, vg, THRESHOLD, min_duration_s=MIN_EVENT_S)
    # NB: no early-return on empty events — every door cycle still counts as
    # dwell (2026-07-29 decision), so the dwell-blob pass below must run.

    bounds = _G["shapes"][asg.shape_id]["seg_bounds"]  # [seg_id, x_lo, x_hi]

    def seg_of(x: float):
        for seg_id, x_lo, x_hi in bounds:
            if x_lo <= x < x_hi:
                return seg_id, x_hi - x  # meters upstream of downstream signal
        return None, None

    vehicle = str(trip["vehicle_id"].iloc[0])
    door = doors.get(vehicle)
    if door is None or len(door) == 0:
        if city.has_door_data:
            # 2026-07-29 decision: vehicle-days without a bus-state extract
            # are DROPPED — without door intervals every stop dwell would be
            # misclassified as non-dwell (red-at-stops artifact).
            rejects["no_door_data"] += 1
            return None
        # No-door city (MBTA): every event flows through unclassified — the
        # zero-overlap path labels them all 'nd', the dwell-blob pass sees
        # no door cycles, and is_last marks the last event per segment. The
        # dashboard renders these as undifferentiated "delay locations".
        door = np.zeros((0, 5))
    trip_key = f"{trip['trip_id'].iloc[0]}_{vehicle}_{date_iso}"
    tz = city.tz

    def x_at(t: float) -> float:
        return float(np.interp(t, tg, xg))

    event_rows: list[dict] = []
    nd_by_seg: Counter = Counter()
    dwell_by_seg: Counter = Counter()
    pax_by_seg: Counter = Counter()

    def load_asof(t_abs: float) -> float:
        # load carried at t = passenger_load of the most recent door CLOSE
        if door is None:
            return 0.0
        prior = door[door[:, 1] <= t_abs]
        return float(prior[-1, 2]) if len(prior) else 0.0

    bnd = {b[0]: (b[1], b[2]) for b in bounds}

    def emit(cls: str, ta: float, tb: float, seg_off=None, stop_id=None):
        # seg_off: explicit (seg_id, off_m) anchor — used by dw rows, which
        # are located at the RAW door coordinates snapped to the shape
        # (2026-08-05 decision) instead of the trajectory midpoint.
        if seg_off is None:
            xm = (x_at(ta - t0_epoch) + x_at(tb - t0_epoch)) / 2
            seg_id, off = seg_of(xm)
        else:
            seg_id, off = seg_off
        if seg_id is None:
            return
        # piece-START position on the same downstream-signal axis
        off_start = bnd[seg_id][1] - x_at(ta - t0_epoch)
        hour = pd.Timestamp(ta, unit="s", tz="UTC").tz_convert(tz).hour
        event_rows.append(
            {
                "seg_id": seg_id, "route_id": route_id,
                "service_date": pd.Timestamp(date_iso).date(),
                "trip_key": trip_key, "cls": cls,
                "off_down_m": float(off), "off_start_m": float(off_start),
                "dur_s": float(tb - ta),
                "hour_local": int(hour),
                "is_last": False, "trip_seq": 0, "is_last_all": False,
                "stop_id": str(stop_id) if stop_id is not None else None,
                "t_start_s": float(ta), "t_end_s": float(tb),
            }
        )

    # Trip-window door cycles (absolute seconds) + aligned stop ids.
    trip_doors = np.empty((0, 5))
    trip_stops: list = []
    if door is not None and len(door):
        t_lo = t0_epoch + float(f.x[0])
        t_hi = t0_epoch + float(f.x[-1])
        mask = (door[:, 0] >= t_lo) & (door[:, 0] <= t_hi)
        trip_doors = door[mask]
        veh_stops = (door_stops or {}).get(vehicle, [])
        trip_stops = ([s for s, m in zip(veh_stops, mask) if m]
                      if len(veh_stops) == len(door)
                      else [None] * len(trip_doors))

    # Door-cycle anchor (2026-08-05): per city.door_anchor —
    #   "raw":      reported door lat/lon snapped onto the assigned shape
    #               (trajectory-at-close fallback for the ~1% off-shape)
    #   "door_mid": trajectory position at the door-interval time-midpoint
    #               (cta-hf high-frequency investigation)
    door_snap: list[tuple] = []
    if len(trip_doors):
        clamp_t = lambda t: min(max(t - t0_epoch, float(f.x[0])), float(f.x[-1]))
        if city.door_anchor == "door_mid":
            for k in range(len(trip_doors)):
                tm = (float(trip_doors[k, 0]) + float(trip_doors[k, 1])) / 2
                door_snap.append(seg_of(x_at(clamp_t(tm))))
        else:
            mtc = _matcher(asg.shape_id)[0]
            snp = mtc.match(trip_doors[:, 3], trip_doors[:, 4], exact_far=False)
            for k in range(len(trip_doors)):
                if snp.on_route[k]:
                    door_snap.append(seg_of(float(snp.dist_along_m[k])))
                else:
                    door_snap.append(seg_of(x_at(clamp_t(float(trip_doors[k, 1])))))

    # ---- non-dwell events + viz shoulders + pax --------------------------
    for ev in events:
        a_abs = t0_epoch + ev.t_start
        b_abs = t0_epoch + ev.t_end
        oidx = (np.where((trip_doors[:, 1] > a_abs)
                         & (trip_doors[:, 0] < b_abs))[0]
                if len(trip_doors) else np.empty(0, int))

        if len(oidx) == 0:
            emit("nd", a_abs, b_abs)
            seg_id, _ = seg_of((ev.x_start + ev.x_end) / 2)
            if seg_id:
                nd_by_seg[seg_id] += ev.duration_s
                pax_by_seg[seg_id] += ev.duration_s * load_asof(a_abs)
        else:
            overl = trip_doors[oidx]
            open_min = float(overl[:, 0].min())
            k_open = int(oidx[int(np.argmin(overl[:, 0]))])
            close_first = float(overl[:, 1].min())
            k_close = int(oidx[int(np.argmin(overl[:, 1]))])
            # >10 s shoulders: viz rows AND the only dwell-side pax pieces.
            # 2026-08-05 rule: a shoulder keeps its pre/post class ONLY when
            # its (trajectory) segment matches its door's RAW segment;
            # otherwise it is a plain nd event, detached from the dwell.
            if open_min - a_abs > PORTION_MIN_S:
                seg_id, _ = seg_of(x_at((a_abs + open_min) / 2 - t0_epoch))
                dseg = door_snap[k_open][0] if door_snap else None
                if seg_id and dseg and seg_id != dseg:
                    emit("nd", a_abs, open_min)
                    nd_by_seg[seg_id] += (open_min - a_abs)
                else:
                    emit("pre", a_abs, open_min, stop_id=trip_stops[k_open])
                if seg_id:
                    pax_by_seg[seg_id] += (open_min - a_abs) * load_asof(a_abs)
            # Post-boarding runs from the FIRST close to the event end
            # (2026-07-31): any further door cycles inside the event are
            # "swallowed" into the piece, which is then classed post2
            # (slashed purple) instead of post.
            if b_abs - close_first > PORTION_MIN_S:
                cls = "post2" if len(oidx) > 1 else "post"
                seg_id, _ = seg_of(x_at((close_first + b_abs) / 2 - t0_epoch))
                dseg = door_snap[k_close][0] if door_snap else None
                if seg_id and dseg and seg_id != dseg:
                    emit("nd", close_first, b_abs)
                    nd_by_seg[seg_id] += (b_abs - close_first)
                else:
                    emit(cls, close_first, b_abs, stop_id=trip_stops[k_close])
                if seg_id:
                    pax_by_seg[seg_id] += (b_abs - close_first) * load_asof(close_first)

    # ---- dwell: EVERY door cycle, unioned with overlapping events --------
    # Merge doors + events-overlapping-doors into connected time blobs so
    # nothing is double counted; attribute each blob by its time-midpoint
    # position. Quick stops (no 15 s event) still contribute their door time.
    if len(trip_doors):
        pieces = [(float(r[0]), float(r[1])) for r in trip_doors]
        for ev in events:
            a_abs = t0_epoch + ev.t_start
            b_abs = t0_epoch + ev.t_end
            if len(trip_doors) and (
                (trip_doors[:, 1] > a_abs) & (trip_doors[:, 0] < b_abs)
            ).any():
                pieces.append((a_abs, b_abs))
        pieces.sort()
        blobs: list[tuple[float, float]] = []
        for lo, hi in pieces:
            if blobs and lo <= blobs[-1][1]:
                blobs[-1] = (blobs[-1][0], max(blobs[-1][1], hi))
            else:
                blobs.append((lo, hi))
        # 2026-08-05: blobs are CUT at segment-boundary crossings (per the
        # trajectory); each door-bearing slice becomes a dw row located at
        # its first door's raw-snapped position and attributed to that raw
        # segment. Door-less slices (queue tails across a boundary) are NOT
        # dwell — their time is carried by the pre/post/nd pieces.
        for lo, hi in blobs:
            kidx = [k for k in range(len(trip_doors))
                    if trip_doors[k, 0] <= hi and trip_doors[k, 1] >= lo]
            if not kidx:
                continue
            x_a, x_b = x_at(lo - t0_epoch), x_at(hi - t0_epoch)
            cuts = sorted(
                t0_epoch + float(np.interp(xh, xg, tg))
                for _sb, _xl, xh in bounds if x_a < xh < x_b)
            edges = [lo] + [c for c in cuts if lo < c < hi] + [hi]
            for pl, ph in zip(edges, edges[1:]):
                pk = [k for k in kidx
                      if pl <= trip_doors[k, 0] < ph
                      or (trip_doors[k, 0] < pl and trip_doors[k, 1] > pl)]
                if not pk or ph <= pl:
                    continue
                seg_id, off = door_snap[pk[0]]
                if seg_id is None:
                    continue
                dwell_by_seg[seg_id] += ph - pl
                # dw annotation row: powers the dwell-cluster median lines
                # in build_distributions. Hidden from the stacked bars.
                emit("dw", pl, ph, seg_off=(seg_id, off),
                     stop_id=trip_stops[pk[0]])

    # Queue markers: per segment, the LAST non-boarding piece before the bus
    # exited (by piece end time). Many sit at the light; a bus released from
    # a queue that clears the rest of the segment leaves its marker upstream.
    # dw rows are boarding by definition and never compete.
    last_by_seg: dict[str, dict] = {}
    for row in event_rows:
        if row["cls"] == "dw":
            continue
        cur = last_by_seg.get(row["seg_id"])
        if cur is None or row["t_end_s"] > cur["t_end_s"]:
            last_by_seg[row["seg_id"]] = row
    for row in last_by_seg.values():
        row["is_last"] = True

    # Generic sequencing + the dw-inclusive last-piece flag (2026-08-05).
    event_rows.sort(key=lambda r: r["t_start_s"])
    for i, row in enumerate(event_rows):
        row["trip_seq"] = i
    last_all: dict[str, dict] = {}
    for row in event_rows:
        cur = last_all.get(row["seg_id"])
        if cur is None or row["t_end_s"] > cur["t_end_s"]:
            last_all[row["seg_id"]] = row
    for row in last_all.values():
        row["is_last_all"] = True

    sum_rows = [
        {
            "seg_id": s, "trip_key": trip_key, "shape_id": asg.shape_id,
            "nd_event_s": float(nd_by_seg.get(s, 0.0)),
            "dwell_union_s": float(dwell_by_seg.get(s, 0.0)),
            "pax_event_s": float(pax_by_seg.get(s, 0.0)),
        }
        for s in set(nd_by_seg) | set(dwell_by_seg) | set(pax_by_seg)
    ]
    return event_rows, sum_rows


def process_date(args):
    city_id, date_iso, force = args[:3]
    if "city" not in _G:
        _init_worker(city_id)
    city: CityConfig = _G["city"]
    base = REPO / "outputs" / "network" / city.city_id
    ev_dir = base / "events" / f"service_date={date_iso}"
    su_dir = base / "event_sums" / f"service_date={date_iso}"
    stats: list[dict] = []

    try:
        df = _service_date_pings(city, date_iso)
        if df.empty:
            return [{"date": date_iso, "route": None, "note": "no_pings"}]
        doors, door_stops = (_door_intervals(city, date_iso)
                             if city.has_door_data else ({}, {}))
        assigned = _stored_assignments(city, date_iso)

        for route_id, route_df in df.groupby("route_id", sort=True):
            out_ev = ev_dir / f"route={route_id}.parquet"
            out_su = su_dir / f"route={route_id}.parquet"
            if out_ev.exists() and out_su.exists() and not force:
                continue
            t0 = time.time()
            rejects: Counter = Counter()
            ev_rows: list[dict] = []
            su_rows: list[dict] = []
            n_kept = 0
            for _, trip in route_df.groupby(["trip_id", "vehicle_id"], sort=False):
                got = _process_trip(trip, date_iso, doors, rejects, assigned,
                                    door_stops)
                if got is None:
                    continue
                ev_rows.extend(got[0])
                su_rows.extend(got[1])
                n_kept += 1
            for d, rows, schema, path in (
                (ev_dir, ev_rows, EVENTS_SCHEMA, out_ev),
                (su_dir, su_rows, SUMS_SCHEMA, out_su),
            ):
                d.mkdir(parents=True, exist_ok=True)
                table = (
                    pa.Table.from_pylist(rows, schema=schema)
                    if rows else schema.empty_table()
                )
                tmp = path.with_suffix(".parquet.tmp")
                pq.write_table(table, tmp, compression="zstd")
                os.replace(tmp, path)
            stats.append(
                {
                    "date": date_iso, "route": str(route_id),
                    "n_trips_kept": n_kept, "n_events": len(ev_rows),
                    "rejects": dict(rejects), "wall_s": round(time.time() - t0, 2),
                }
            )
    except Exception as e:  # noqa: BLE001 — never kill the pool
        import traceback

        stats.append({"date": date_iso, "route": None,
                      "error": f"{type(e).__name__}: {e}",
                      "trace": traceback.format_exc(limit=6)})
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--city", default="cta")
    ap.add_argument("--date", default=None)
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    from analysis.network.run_reconstruct import _dates_in_archive

    city = get_city(args.city)
    if args.date:
        dates = [args.date]
    else:
        dates = _dates_in_archive(city)
        if args.start:
            dates = [d for d in dates if d >= args.start]
        if args.end:
            dates = [d for d in dates if d <= args.end]
    print(f"{len(dates)} service date(s)")

    index_path = REPO / "outputs" / "network" / city.city_id / "events_index.jsonl"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    work = [(args.city, d, args.force) for d in dates]
    t_start = time.time()
    done = 0

    def log(stats):
        nonlocal done
        done += 1
        with open(index_path, "a") as fh:
            for s in stats:
                fh.write(json.dumps(s) + "\n")
        kept = sum(s.get("n_trips_kept", 0) for s in stats)
        nev = sum(s.get("n_events", 0) for s in stats)
        errs = [s for s in stats if s.get("error")]
        suffix = f"  !! {len(errs)} ERROR(S)" if errs else ""
        date = stats[0]["date"] if stats else "?"
        print(f"[{done}/{len(dates)}] {date}: {kept} trips, {nev} events "
              f"({time.time()-t_start:.0f}s){suffix}", flush=True)

    if args.workers <= 1:
        _init_worker(args.city)
        for w in work:
            log(process_date(w))
    else:
        with Pool(args.workers, initializer=_init_worker, initargs=(args.city,)) as pool:
            for stats in pool.imap_unordered(process_date, work):
                log(stats)
    print("done")


if __name__ == "__main__":
    main()
