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

Each classified row gets a LOCATION: the along-road midpoint of the relevant
portion, expressed as meters upstream of the segment's DOWNSTREAM signal
(queues at the light cluster near 0).

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
        ("cls", pa.dictionary(pa.int32(), pa.string())),  # nd | pre | post
        ("off_down_m", pa.float32()),  # midpoint, meters upstream of downstream signal
        ("dur_s", pa.float32()),
        ("hour_local", pa.uint8()),
        ("is_last", pa.bool_()),  # queue marker: traversal's last piece in seg
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


def _door_intervals(city: CityConfig, date_iso: str) -> dict[str, np.ndarray]:
    """vehicle -> array[[t_open_utc_s, t_close_utc_s, load], ...] sorted."""
    import duckdb

    ev_glob = str(city.resolve("caches/door_events") / city.city_id / "*.parquet")
    cut = city.service_day_cutover_h
    con = duckdb.connect()
    rows = con.execute(
        f"""
        SELECT bus_id,
               epoch((event_time AT TIME ZONE '{city.tz}')) AS t_open,
               dwell_s, passenger_load
        FROM read_parquet('{ev_glob}')
        WHERE (event_time - INTERVAL {cut} HOUR)::DATE = DATE '{date_iso}'
        ORDER BY bus_id, t_open
        """
    ).fetchall()
    out: dict[str, list] = defaultdict(list)
    for bus, t_open, dwell, load in rows:
        out[str(bus)].append((float(t_open), float(t_open) + float(dwell or 0.0),
                              min(int(load or 0), MAX_LOAD)))
    return {k: np.asarray(v) for k, v in out.items()}


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
                  assigned: dict[str, str] | None = None):
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
        door = np.zeros((0, 3))
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

    def emit(cls: str, ta: float, tb: float):
        xm = (x_at(ta - t0_epoch) + x_at(tb - t0_epoch)) / 2
        seg_id, off = seg_of(xm)
        if seg_id is None:
            return
        hour = pd.Timestamp(ta, unit="s", tz="UTC").tz_convert(tz).hour
        event_rows.append(
            {
                "seg_id": seg_id, "route_id": route_id,
                "service_date": pd.Timestamp(date_iso).date(),
                "trip_key": trip_key, "cls": cls,
                "off_down_m": float(off), "dur_s": float(tb - ta),
                "hour_local": int(hour),
                "is_last": False, "_t_end": tb,  # stripped before write
            }
        )

    # Trip-window door cycles (absolute seconds).
    trip_doors = np.empty((0, 3))
    if door is not None:
        t_lo = t0_epoch + float(f.x[0])
        t_hi = t0_epoch + float(f.x[-1])
        trip_doors = door[(door[:, 0] >= t_lo) & (door[:, 0] <= t_hi)]

    # ---- non-dwell events + viz shoulders + pax --------------------------
    for ev in events:
        a_abs = t0_epoch + ev.t_start
        b_abs = t0_epoch + ev.t_end
        overl = trip_doors[
            (trip_doors[:, 1] > a_abs) & (trip_doors[:, 0] < b_abs)
        ] if len(trip_doors) else trip_doors

        if len(overl) == 0:
            emit("nd", a_abs, b_abs)
            seg_id, _ = seg_of((ev.x_start + ev.x_end) / 2)
            if seg_id:
                nd_by_seg[seg_id] += ev.duration_s
                pax_by_seg[seg_id] += ev.duration_s * load_asof(a_abs)
        else:
            open_min = float(overl[:, 0].min())
            close_max = float(overl[:, 1].max())
            # >10 s shoulders: viz rows AND the only dwell-side pax pieces
            if open_min - a_abs > PORTION_MIN_S:
                emit("pre", a_abs, open_min)
                seg_id, _ = seg_of(x_at((a_abs + open_min) / 2 - t0_epoch))
                if seg_id:
                    pax_by_seg[seg_id] += (open_min - a_abs) * load_asof(a_abs)
            if b_abs - close_max > PORTION_MIN_S:
                emit("post", close_max, b_abs)
                seg_id, _ = seg_of(x_at((close_max + b_abs) / 2 - t0_epoch))
                if seg_id:
                    pax_by_seg[seg_id] += (b_abs - close_max) * load_asof(close_max)

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
        for lo, hi in blobs:
            seg_id, _ = seg_of(x_at((lo + hi) / 2 - t0_epoch))
            if seg_id:
                dwell_by_seg[seg_id] += hi - lo

    # Queue markers: per segment, the LAST non-boarding piece before the bus
    # exited (by piece end time). Many sit at the light; a bus released from
    # a queue that clears the rest of the segment leaves its marker upstream.
    last_by_seg: dict[str, dict] = {}
    for row in event_rows:
        cur = last_by_seg.get(row["seg_id"])
        if cur is None or row["_t_end"] > cur["_t_end"]:
            last_by_seg[row["seg_id"]] = row
    for row in last_by_seg.values():
        row["is_last"] = True
    for row in event_rows:
        row.pop("_t_end", None)

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
        doors = _door_intervals(city, date_iso) if city.has_door_data else {}
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
                got = _process_trip(trip, date_iso, doors, rejects, assigned)
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
