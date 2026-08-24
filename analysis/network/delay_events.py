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
    _set_era,
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
from core.smooth import fit_trajectory  # noqa: E402
from dataio.cities import CityConfig, get_city  # noqa: E402

THRESHOLD = AbsoluteSpeedThreshold(5.0)   # overridden by --mph
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
        # Stop attribution of the associated door cycle (dw/pre/
        # post/post2 rows; null for nd). Powers per-stop performance stats.
        ("stop_id", pa.string()),
        # Piece time bounds (epoch s), for trip-sequencing analyses.
        ("t_start_s", pa.float64()),
        ("t_end_s", pa.float64()),
        # Passenger load carried at piece start (load_asof: passenger_load
        # of the most recent door close). Powers the passenger-seconds
        # distribution tab: bucket pax·s = sum(dur_s × pax).
        ("pax", pa.float32()),
        # post/post2 whose attributed stop is NEAR-SIDE: delay there is a
        # stop-then-signal compound, split out in the distributions as
        # post_ns/post2_ns (2026-08-21).
        ("near_side", pa.bool_()),
    ]
)

TRAJ_SPEED_SCHEMA = pa.schema(
    [
        ("seg_id", pa.dictionary(pa.int32(), pa.string())),
        ("bucket", pa.int32()),   # 10 ft buckets upstream of downstream signal
        ("n", pa.int64()),        # traversals fully crossing the bucket
        ("sum_dt", pa.float64()), # summed crossing seconds (dwell included)
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

    cut = city.service_day_cutover_h
    con = duckdb.connect()
    if city.door_source_dir:
        # Historical monthly export: one file per month, no stop_id, dwell
        # named dwell_time. A service date can straddle two months, so read
        # this month and the previous one and let the WHERE clause cut it.
        d0 = pd.Timestamp(date_iso)
        months = {d0.strftime("%Y%m"),
                  (d0 - pd.Timedelta(days=1)).strftime("%Y%m"),
                  (d0 + pd.Timedelta(days=1)).strftime("%Y%m")}
        src = Path(city.door_source_dir)
        files = [src / f"month={m}.parquet" for m in sorted(months)]
        files = [f for f in files if f.exists()]
        if not files:
            return {}, {}
        ev_glob = ", ".join(f"'{f}'" for f in files)
        cols = set(pq.ParquetFile(files[0]).schema.names)
        dwell = "dwell_s" if "dwell_s" in cols else "dwell_time"
        stop = "stop_id" if "stop_id" in cols else "NULL"
        src_sql = f"read_parquet([{ev_glob}], union_by_name=true)"
    else:
        ev_glob = str(city.resolve("caches/door_events") / city.city_id / "*.parquet")
        dwell, stop = "dwell_s", "stop_id"
        src_sql = f"read_parquet('{ev_glob}')"
    rows = con.execute(
        f"""
        SELECT bus_id,
               epoch((event_time AT TIME ZONE '{city.tz}')) AS t_open,
               {dwell} AS dwell_s, passenger_load, latitude, longitude,
               {stop} AS stop_id,
               CAST(trip_id AS VARCHAR) || '|'
                 || CAST(trip_start_time AS VARCHAR) AS dtrip
        FROM {src_sql}
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
    # Terminal layover: dwell_time on a trip's first/last active event is the
    # layover, not door-open time (4.6% of cycles exceed 300 s and ~94% of
    # those sit at a trip boundary; max observed 9 hours). Left in they
    # inflate dwell_union_s and swallow real stops. sanitize_cycles zeroes
    # the bogus duration, keeping the boarding counts.
    from core.decompose.door_delay import sanitize_cycles

    raw: dict[str, list] = defaultdict(list)
    for bus, t_open, dwell, load, lat, lon, stop_id, dtrip in rows:
        raw[str(bus)].append({
            "open": float(t_open), "close": float(t_open) + float(dwell or 0.0),
            "load": min(int(load or 0), MAX_LOAD),
            "lat": float(lat or 0.0), "lon": float(lon or 0.0),
            "stop_id": stop_id, "trip_key": dtrip})
    for bus, cyc in raw.items():
        for c in sanitize_cycles(cyc):
            # Drop the layover outright: zero-width it still OVERLAPS the
            # terminal's long slow event, anchoring a dwell blob and a post
            # that span the whole recovery time. Scheduled recovery is not
            # passenger delay.
            if c.get("layover_trimmed"):
                continue
            out[bus].append((c["open"], c["close"], c["load"], c["lat"], c["lon"]))
            stops[bus].append(c["stop_id"])
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


def _pattern_stops(shape_id: str):
    """(sorted dists_along, stop_ids) for the shape's stops; None if none.

    Stop offsets are per-segment (m upstream of the segment end); each
    segment's end position on this shape comes from seg_bounds.
    """
    cache = _G.setdefault("pattern_stops", {})
    if shape_id in cache:
        return cache[shape_id]
    rec = _G["shapes"].get(shape_id)
    out = None
    if rec is not None:
        pts = []
        for seg_id, _x_lo, x_hi in rec["seg_bounds"]:
            for off_m, stop_id in _G.get("seg_stops", {}).get(seg_id, []):
                pts.append((float(x_hi) - off_m, stop_id))
        if pts:
            pts.sort()
            out = (np.array([p[0] for p in pts]), [p[1] for p in pts])
    cache[shape_id] = out
    return out


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
    v_all = (trip["speed_mps"].to_numpy(dtype=float)
             if "speed_mps" in trip.columns else None)
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
    v_on = v_all[on] if v_all is not None else None
    if monotone_frac(d_on) < MIN_MONOTONE:
        rejects["not_monotone"] += 1
        return None
    at_term = np.nonzero(d_on >= shape_len - TERMINAL_M)[0]
    if len(at_term):
        cut = at_term[0] + 1
        t_on, d_on = t_on[:cut], d_on[:cut]
        if v_on is not None:
            v_on = v_on[:cut]
        if len(t_on) < MIN_PINGS:
            rejects["few_pings_after_truncate"] += 1
            return None
    try:
        sm = fit_trajectory(t_on, d_on, v_on)
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

    # ---- trajectory bucket-crossing times (--traj-speed) -----------------
    # Per 10 ft bucket, the time this trajectory took to cross it (dwell
    # included): bucket avg speed = len / mean(dt), the L/avg-crossing-time
    # estimator. Immune to the stop-zone milestone pings that bias the
    # ping-speed bucket mean (positions stamped at fixed points).
    ts_rows: list[tuple] = []
    if _G.get("traj_speed"):
        bucket_m = 10.0 / 3.28084
        x_cov_lo, x_cov_hi = float(xg[0]), float(xg[-1])
        for seg_id, x0, x1 in bounds:
            if x1 <= x_cov_lo or x0 >= x_cov_hi:
                continue
            nb = int(np.ceil((x1 - x0) / bucket_m))
            bx = x1 - np.arange(nb + 1) * bucket_m
            bx[-1] = x0  # upstream bucket keeps its true (shorter) length
            ok = (bx >= x_cov_lo) & (bx <= x_cov_hi)
            if ok.sum() < 2:
                continue
            bxo = bx[ok]
            # first time the (nondecreasing) trajectory reaches each boundary
            idx = np.searchsorted(xg, bxo, side="left")
            t_at = np.full(len(bxo), np.nan)
            t_at[idx == 0] = tg[0]
            inner = (idx > 0) & (idx < len(xg))
            ii = idx[inner]
            xa, xb = xg[ii - 1], xg[ii]
            with np.errstate(divide="ignore", invalid="ignore"):
                frac = np.where(xb > xa, (bxo[inner] - xa) / (xb - xa), 0.0)
            t_at[inner] = tg[ii - 1] + frac * (tg[ii] - tg[ii - 1])
            t_full = np.full(nb + 1, np.nan)
            t_full[ok] = t_at
            # bucket k spans bx[k+1]..bx[k]; the downstream boundary bx[k]
            # is reached later, so dt_k = t(bx[k]) - t(bx[k+1])
            dt = t_full[:-1] - t_full[1:]
            for k in np.nonzero(np.isfinite(dt) & (dt > 0))[0]:
                ts_rows.append((seg_id, int(k), float(dt[k])))

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
                "pax": float(load_asof(ta)),
                "near_side": bool(
                    cls in ("post", "post2") and stop_id is not None
                    and str(stop_id) in _G.get("near_side_stops", ())),
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
    door_x: list[float] = []          # along-shape position of each cycle
    if len(trip_doors):
        clamp_t = lambda t: min(max(t - t0_epoch, float(f.x[0])), float(f.x[-1]))
        if city.door_anchor == "door_mid":
            for k in range(len(trip_doors)):
                tm = (float(trip_doors[k, 0]) + float(trip_doors[k, 1])) / 2
                x = x_at(clamp_t(tm))
                door_x.append(float(x))
                door_snap.append(seg_of(x))
        else:
            mtc = _matcher(asg.shape_id)[0]
            snp = mtc.match(trip_doors[:, 3], trip_doors[:, 4], exact_far=False)
            for k in range(len(trip_doors)):
                if snp.on_route[k]:
                    x = float(snp.dist_along_m[k])
                else:
                    x = float(x_at(clamp_t(float(trip_doors[k, 1]))))
                door_x.append(x)
                door_snap.append(seg_of(x))

    # Location-based re-attribution (2026-08-16 decision): each door cycle
    # is assigned to the NEAREST stop on the trip's pattern (the assigned
    # shape's stop set, positioned along the shape), ignoring the AVL
    # system's stop_id stamp — which lags on skips/bays/stations (the
    # 6515/6137 bleed class). Modal stop locations themselves stay
    # stamp-derived upstream in the registry. Falls back to the stamp only
    # when the shape carries no stops.
    if len(trip_doors):
        pattern = _pattern_stops(asg.shape_id)
        if pattern is not None:
            p_dists, p_ids = pattern
            idx = np.searchsorted(p_dists, np.asarray(door_x))
            reattr = []
            for k, j in enumerate(idx):
                lo = max(0, j - 1)
                hi = min(len(p_dists) - 1, j)
                best = lo if (abs(door_x[k] - p_dists[lo])
                              <= abs(door_x[k] - p_dists[hi])) else hi
                reattr.append(p_ids[best])
            trip_stops = reattr

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
                # dw annotation row: powers the door-events distribution
                # layer in build_distributions. Hidden from the stacked
                # bars unless the checkbox is on.
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
    return event_rows, sum_rows, ts_rows


def _init_worker_ev(city_id: str, mph: float = 5.0, suffix: str = "",
                    traj_speed: bool = False) -> None:
    """Shared initializer plus the threshold/suffix this pass runs at."""
    _init_worker(city_id)
    global THRESHOLD
    THRESHOLD = AbsoluteSpeedThreshold(mph)
    _G["out_suffix"] = suffix
    _G["traj_speed"] = traj_speed


def process_date(args):
    city_id, date_iso, force = args[:3]
    mph = args[3] if len(args) > 3 else 5.0
    suffix = args[4] if len(args) > 4 else ""
    traj_speed = args[5] if len(args) > 5 else False
    if "city" not in _G:
        _init_worker_ev(city_id, mph, suffix, traj_speed)
    city: CityConfig = _G["city"]
    _set_era(date_iso)
    base = REPO / "outputs" / "network" / city.city_id
    suffix = _G.get("out_suffix", "")
    ev_dir = base / f"events{suffix}" / f"service_date={date_iso}"
    su_dir = base / f"event_sums{suffix}" / f"service_date={date_iso}"
    ts_dir = base / "traj_speed" / f"service_date={date_iso}"
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
            if (out_ev.exists() and out_su.exists() and not force
                    and not (_G.get("traj_speed")
                             and not (ts_dir / f"route={route_id}.parquet").exists())):
                continue
            t0 = time.time()
            rejects: Counter = Counter()
            ev_rows: list[dict] = []
            su_rows: list[dict] = []
            ts_n: Counter = Counter()
            ts_dt: Counter = Counter()
            n_kept = 0
            for _, trip in route_df.groupby(["trip_id", "vehicle_id"], sort=False):
                got = _process_trip(trip, date_iso, doors, rejects, assigned,
                                    door_stops)
                if got is None:
                    continue
                ev_rows.extend(got[0])
                su_rows.extend(got[1])
                for s_, b_, dt_ in got[2]:
                    ts_n[(s_, b_)] += 1
                    ts_dt[(s_, b_)] += dt_
                n_kept += 1
            sinks = [
                (ev_dir, ev_rows, EVENTS_SCHEMA, out_ev),
                (su_dir, su_rows, SUMS_SCHEMA, out_su),
            ]
            if _G.get("traj_speed"):
                ts_rows = [
                    {"seg_id": s_, "bucket": b_, "n": ts_n[(s_, b_)],
                     "sum_dt": ts_dt[(s_, b_)]}
                    for (s_, b_) in ts_n
                ]
                sinks.append((ts_dir, ts_rows, TRAJ_SPEED_SCHEMA,
                              ts_dir / f"route={route_id}.parquet"))
            for d, rows, schema, path in sinks:
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
    ap.add_argument("--mph", type=float, default=5.0,
                    help="slow-event speed threshold; non-default values "
                         "write to events<suffix>/ (e.g. --mph 3 -> events3mph/)")
    ap.add_argument("--date", default=None)
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--traj-speed", action="store_true",
                    help="also write traj_speed/ per-bucket crossing times "
                         "(threshold-independent; run with one pass only)")
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

    out_suffix = "" if args.mph == 5.0 else f"{args.mph:g}mph"
    index_path = (REPO / "outputs" / "network" / city.city_id
                  / f"events_index{out_suffix}.jsonl")
    index_path.parent.mkdir(parents=True, exist_ok=True)
    work = [(args.city, d, args.force, args.mph, out_suffix, args.traj_speed)
            for d in dates]
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
        _init_worker_ev(args.city, args.mph, out_suffix, args.traj_speed)
        for w in work:
            log(process_date(w))
    else:
        with Pool(args.workers, initializer=_init_worker_ev,
                  initargs=(args.city, args.mph, out_suffix,
                            args.traj_speed)) as pool:
            for stats in pool.imap_unordered(process_date, work):
                log(stats)
    print("done")


if __name__ == "__main__":
    main()
