"""Corrected per-service-date pings for feeds that need repair before reconstruction.

Cities with ``CityConfig.r2_sources`` (NYC) do not feed raw R2 hour-files to
the batches. This stage reads them once and writes one parquet per service
date to ``city.ping_dir``; every later stage reads only those files.

Three repairs, in order:

1. **First-seen row per (vehicle, timestamp).** The scraper polls faster than
   buses report, so one report is archived from several feed builds. Rows are
   sorted by (vehicle, timestamp, source rank, feed build time) and the first
   kept: an observed (SIRI) row wins over an extrapolated one, and among
   extrapolated rows the earliest build carries the least extrapolation.

2. **Timetable reversal** — only for rows from an ``oba_extrapolated`` source
   (MTA GTFS-rt vehiclePositions). OneBusAway publishes the timetable position
   at ``s_obs + (T - t_obs)``: the last real report pushed along the trip's
   stop_times to the feed build time ``T``, while ``timestamp`` stays
   ``t_obs``. The reversal projects the published point onto the trip's
   shape, inverts the trip's distance-vs-scheduled-time curve, steps back by
   ``T - t_obs - 1 s`` and reads the distance there. Scored against SIRI/REST
   reports with the same timestamp (2026-11-20): median error 1 ft, 97% within
   25 ft; as archived the median is +130 ft. The 1 s is an empirical
   calibration (bias -7 ft without it). Rows whose trip is in no nearby GTFS
   era cannot be reversed and are dropped, not kept biased. ``observed`` rows
   are never moved.

3. **Trip identity by vehicle run.** The MTA feed's trip_id flips between
   adjacent scheduled trips mid-run on ~12% of runs, so grouping by trip_id
   splits one physical trip and counts buses twice. Pings are regrouped into
   runs: same vehicle, route and direction, no gap over ``GAP_S``; a trip_id
   change starts a new run only where the bus is at a terminal of either
   trip or has moved backwards. ``trip_id`` in the output is the run id (the
   run's first feed trip_id, suffixed ``~k`` when a vehicle repeats it);
   ``feed_trip_id`` keeps the original.

Era timetables (stop distances along each trip's shape + scheduled times) are
built once per GTFS era into ``outputs/network/<city>/era_timetables/``.

Usage:
    PYTHONPATH=src uv run python analysis/network/ping_ingest.py --city nyc --workers 6
    ... --date 2026-09-22 --force
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import zipfile
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from analysis.network import gtfs_history  # noqa: E402
from core.mapmatch.shape_snap import SnapToShapeMatcher  # noqa: E402
from dataio.cities import CityConfig, get_city  # noqa: E402
from dataio.gtfs import load_gtfs_shape_with_dist  # noqa: E402

EXTRAPOLATED = "oba_extrapolated"
AGE_OFFSET_S = 1.0     # empirical: feed build stamp runs ~1 s behind the extrapolation target
PERP_OK_M = 15.0       # published points lie on the trip's shape; farther = wrong shape/era
GAP_S = 300.0          # a silent gap this long ends a run
TERM_M = 300.0         # "at a terminal" for the trip_id-change rule
BACK_M = 300.0         # trip_id change + this much backward movement = new trip
PAD_BEFORE_H = 4       # read this far before the service day so runs keep their start
PAD_AFTER_H = 1
RAW_COLUMNS = ("timestamp", "feed_timestamp", "trip_id", "route_id",
               "direction_id", "vehicle_id", "latitude", "longitude",
               "vehicle_status")

OUT_SCHEMA = pa.schema([
    ("timestamp", pa.timestamp("ms", tz="UTC")),
    ("trip_id", pa.string()),
    ("feed_trip_id", pa.string()),
    ("route_id", pa.dictionary(pa.int32(), pa.string())),
    ("direction_id", pa.int8()),
    ("vehicle_id", pa.dictionary(pa.int32(), pa.string())),
    ("latitude", pa.float64()),
    ("longitude", pa.float64()),
    ("source", pa.dictionary(pa.int32(), pa.string())),
    ("service_date", pa.date32()),
])


# --------------------------------------------------------------------------
# Era timetables
# --------------------------------------------------------------------------

def _tt_dir(city: CityConfig) -> Path:
    return REPO / "outputs" / "network" / city.city_id / "era_timetables"


def _era_zip(city: CityConfig, era: str | None) -> Path:
    if era is None:
        return city.resolve(city.gtfs_zip)
    return city.resolve(city.gtfs_history_dir) / f"{era}.zip"


_MATCHERS: dict[tuple[str, str], SnapToShapeMatcher | None] = {}


def _matcher(zip_path: Path, shape_id: str, max_perp_m: float) -> SnapToShapeMatcher | None:
    key = (str(zip_path), shape_id)
    if key not in _MATCHERS:
        try:
            poly, dist = load_gtfs_shape_with_dist(zip_path, shape_id)
            _MATCHERS[key] = SnapToShapeMatcher(
                poly, max_perp_m=max_perp_m, dist_along_m_per_vertex=dist,
                sequential=True)
        except (KeyError, ValueError):
            _MATCHERS[key] = None
    return _MATCHERS[key]


def build_timetable(city: CityConfig, era: str | None, force: bool = False) -> Path:
    """Stop distance along the trip's shape + scheduled times, per trip.

    Two files: ``<era>.trips.parquet`` (trip_id, shape_id, off, n — sorted by
    trip_id) and ``<era>.stops.parquet`` (d_m, arr_s, dep_s in trip order;
    a trip's stops are rows off..off+n). Stops are projected once per
    distinct (shape, stop sequence) with the loop-aware sequential matcher,
    so a route that revisits a street keeps its stops in travel order.
    """
    import duckdb

    out = _tt_dir(city)
    out.mkdir(parents=True, exist_ok=True)
    name = era or "canonical"
    p_trips, p_stops = out / f"{name}.trips.parquet", out / f"{name}.stops.parquet"
    if p_trips.exists() and p_stops.exists() and not force:
        return p_trips
    zp = _era_zip(city, era)
    t0 = time.time()
    with tempfile.TemporaryDirectory() as td, zipfile.ZipFile(zp) as z:
        for m in ("stop_times.txt", "trips.txt", "stops.txt"):
            z.extract(m, td)
        con = duckdb.connect()
        sec = ("3600*split_part({c},':',1)::int + 60*split_part({c},':',2)::int"
               " + split_part({c},':',3)::int")
        con.execute(f"""
            create table st as
            select trip_id, stop_id, stop_sequence::int seq,
                   {sec.format(c='arrival_time')} arr, {sec.format(c='departure_time')} dep
            from read_csv('{td}/stop_times.txt', all_varchar=true)
            where arrival_time <> '' and departure_time <> ''""")
        con.execute(f"""
            create table tr as
            select t.trip_id, t.shape_id,
                   list(s.stop_id order by s.seq) stops
            from read_csv('{td}/trips.txt', all_varchar=true) t
            join st s using (trip_id)
            where t.shape_id is not null and t.shape_id <> ''
            group by 1, 2""")
        con.execute("""
            create table pat as
            select row_number() over () pid, shape_id, stops
            from (select distinct shape_id, stops from tr)""")
        stops = con.execute(
            f"select stop_id, stop_lat::double lat, stop_lon::double lon "
            f"from read_csv('{td}/stops.txt', all_varchar=true)").df().set_index("stop_id")
        pats = con.execute("select pid, shape_id, stops from pat").df()
        pid_l, pos_l, d_l = [], [], []
        n_bad = 0
        for pid, sid, sl in zip(pats.pid, pats.shape_id, pats.stops):
            m = _matcher(zp, sid, city.max_perp_m)
            ll = stops.reindex(list(sl))
            if m is None or ll.lat.isna().any():
                n_bad += 1
                continue
            d = m.match(ll.lat.to_numpy(), ll.lon.to_numpy()).dist_along_m
            d = np.maximum.accumulate(d)
            pid_l.append(np.full(len(d), pid))
            pos_l.append(np.arange(1, len(d) + 1))
            d_l.append(d)
        pd_df = pd.DataFrame({"pid": np.concatenate(pid_l), "pos": np.concatenate(pos_l),
                              "d_m": np.concatenate(d_l)})
        con.register("pd_df", pd_df)
        con.execute("""
            create table full_st as
            select s.trip_id, s.seq, p.d_m, s.arr, s.dep
            from (select *, row_number() over (partition by trip_id order by seq) pos
                  from st) s
            join tr using (trip_id)
            join pat on pat.shape_id = tr.shape_id and pat.stops = tr.stops
            join pd_df p on p.pid = pat.pid and p.pos = s.pos""")
        full = con.execute(
            "select trip_id, d_m, arr, dep from full_st order by trip_id, seq").to_arrow_table()
        shape_of = con.execute("select trip_id, shape_id from tr order by trip_id").df()
    tid = full.column("trip_id").to_pandas()
    counts = tid.groupby(tid, sort=True).size()
    trips = shape_of[shape_of.trip_id.isin(counts.index)].reset_index(drop=True)
    trips["n"] = counts.reindex(trips.trip_id).to_numpy().astype(np.int32)
    trips["off"] = (np.cumsum(trips.n.to_numpy()) - trips.n.to_numpy()).astype(np.int64)
    pq.write_table(pa.table({
        "d_m": full.column("d_m").cast(pa.float32()),
        "arr_s": full.column("arr").cast(pa.int32()),
        "dep_s": full.column("dep").cast(pa.int32()),
    }), str(p_stops) + ".tmp", compression="zstd")
    os.replace(str(p_stops) + ".tmp", p_stops)
    trips.to_parquet(str(p_trips) + ".tmp", index=False)
    os.replace(str(p_trips) + ".tmp", p_trips)
    print(f"timetable {name}: {len(trips):,} trips, {full.num_rows:,} stop rows, "
          f"{len(pats):,} patterns ({n_bad} unprojectable) in {time.time() - t0:.0f}s",
          flush=True)
    return p_trips


class Timetable:
    """One era's trips in memory: trip_id -> shape + (distance, time) curve."""

    def __init__(self, city: CityConfig, era: str | None):
        name = era or "canonical"
        d = _tt_dir(city)
        trips = pd.read_parquet(d / f"{name}.trips.parquet")
        st = pq.read_table(d / f"{name}.stops.parquet")
        self.era = era
        self.zip = _era_zip(city, era)
        self.index = pd.Index(trips.trip_id)
        self.shape = trips.shape_id.to_numpy(dtype=object)
        self.off = trips.off.to_numpy()
        self.n = trips.n.to_numpy()
        dm = st.column("d_m").to_numpy().astype(np.float64)
        # Curve points per stop: (d, arrival) then (d, departure).
        self.dd = np.repeat(dm, 2)
        self.tt = np.empty(2 * len(dm))
        self.tt[0::2] = st.column("arr_s").to_numpy()
        self.tt[1::2] = st.column("dep_s").to_numpy()

    def curve(self, k: int) -> tuple[np.ndarray, np.ndarray]:
        lo = 2 * self.off[k]
        hi = lo + 2 * self.n[k]
        return self.dd[lo:hi], np.maximum.accumulate(self.tt[lo:hi])


_TT: dict[tuple[str, str | None], Timetable] = {}


def _timetable(city: CityConfig, era: str | None) -> Timetable:
    key = (city.city_id, era)
    if key not in _TT:
        while len(_TT) >= 3:           # a worker rarely needs more than 3 eras
            _TT.pop(next(iter(_TT)))
        _TT[key] = Timetable(city, era)
    return _TT[key]


def _eras_near(city: CityConfig, date_iso: str) -> list[str | None]:
    """The date's era first, then the eras either side of it (trips of a
    feed that changed overnight, and the pre-cutover pad hours)."""
    if not city.gtfs_history_dir:
        return [None]
    idx = json.loads((city.resolve(city.gtfs_history_dir) / "index.json").read_text())
    dates = idx.get("dates", {})
    if date_iso not in dates:
        return [None]
    # consecutive runs of one era, in date order
    runs: list[list[str]] = []          # [era, first_date, last_date]
    for d in sorted(dates):
        if runs and runs[-1][0] == dates[d]:
            runs[-1][2] = d
        else:
            runs.append([dates[d], d, d])
    i = next(j for j, r in enumerate(runs) if r[1] <= date_iso <= r[2])
    out = [runs[i][0]]
    for j in (i - 1, i + 1, i - 2, i + 2):
        if 0 <= j < len(runs) and runs[j][0] not in out:
            out.append(runs[j][0])
    have = _tt_dir(city)
    return [e for e in out if (have / f"{e}.trips.parquet").exists()]


# --------------------------------------------------------------------------
# Projection + reversal
# --------------------------------------------------------------------------

def _project(city: CityConfig, tt: Timetable, shape: np.ndarray, grp: np.ndarray,
             lat: np.ndarray, lon: np.ndarray):
    """Distance along each row's own trip shape.

    Rows must be in time order within ``grp`` (vehicle x trip). Shapes that
    revisit a street are matched group by group with the sequential matcher
    so a bus on the second pass is not placed on the first.
    Returns (d, perp, shape_len).
    """
    n = len(lat)
    d = np.full(n, np.nan)
    perp = np.full(n, np.inf)
    slen = np.full(n, np.nan)
    for sid, idx in pd.Series(shape).groupby(shape, sort=False).indices.items():
        m = _matcher(tt.zip, sid, city.max_perp_m)
        if m is None:
            continue
        slen[idx] = m.total_length_m
        if m.self_overlaps:
            g = grp[idx]
            cuts = np.flatnonzero(np.r_[True, g[1:] != g[:-1], True])
            parts = [idx[a:b] for a, b in zip(cuts[:-1], cuts[1:])]
        else:
            parts = [idx[a:a + 20000] for a in range(0, len(idx), 20000)]
        for part in parts:
            r = m.match(lat[part], lon[part], exact_far=False)
            d[part] = r.dist_along_m
            perp[part] = r.perp_dist_m
    return d, perp, slen


def _sched_time(dd: np.ndarray, tt_: np.ndarray, d: np.ndarray) -> np.ndarray:
    """Inverse of the timetable curve: scheduled time at distance ``d``.

    Between two stops the bus runs from the DEPARTURE of the first to the
    arrival at the second, so a scheduled dwell is not smeared into the
    running segment after it. Exactly at a stop (time ambiguous over the
    dwell) the arrival is used.
    """
    first = np.r_[True, np.diff(dd) > 0]          # first curve point of each distinct distance
    last = np.r_[first[1:], True]
    D, arr, dep = dd[first], tt_[first], tt_[last]
    if len(D) < 2:
        return np.full(len(d), arr[0])
    j = np.clip(np.searchsorted(D, d, side="right") - 1, 0, len(D) - 2)
    frac = np.clip((d - D[j]) / (D[j + 1] - D[j]), 0.0, 1.0)
    s = dep[j] + frac * (arr[j + 1] - dep[j])
    return np.where(frac <= 0.0, arr[j], s)


def reverse_positions(tt: Timetable, trip_k: np.ndarray, d_pub: np.ndarray,
                      age_s: np.ndarray) -> np.ndarray:
    """Distance of the real report: step ``age_s`` back along each trip's timetable."""
    out = np.full(len(d_pub), np.nan)
    order = np.argsort(trip_k, kind="stable")
    ks = trip_k[order]
    cuts = np.flatnonzero(np.r_[True, ks[1:] != ks[:-1], True])
    for a, b in zip(cuts[:-1], cuts[1:]):
        idx = order[a:b]
        dd, tt_ = tt.curve(int(ks[a]))
        out[idx] = np.interp(_sched_time(dd, tt_, d_pub[idx]) - age_s[idx], tt_, dd)
    return out


def _latlon_at(city: CityConfig, tt: Timetable, shape: np.ndarray, d: np.ndarray):
    lat = np.full(len(d), np.nan)
    lon = np.full(len(d), np.nan)
    for sid, idx in pd.Series(shape).groupby(shape, sort=False).indices.items():
        m = _matcher(tt.zip, sid, city.max_perp_m)
        if m is None:
            continue
        cum = m._cum_at_vert
        lat[idx] = np.interp(d[idx], cum, m.polyline[:, 0])
        lon[idx] = np.interp(d[idx], cum, m.polyline[:, 1])
    return lat, lon


def correct(city: CityConfig, df: pd.DataFrame, date_iso: str) -> tuple[pd.DataFrame, dict]:
    """Project every row on its trip's shape; reverse the extrapolated ones.

    ``df``: vehicle_id, ts (epoch s), hdr (epoch s), trip_id, latitude,
    longitude, extrap (bool), sorted by (vehicle_id, ts). Adds d_m (distance
    along the trip's shape after correction), shape_len_m, shape_key (index
    into ``out.attrs["shape_keys"]`` = (era, shape_id); -1 unknown); moves
    latitude/longitude of extrapolated rows; drops extrapolated rows that
    cannot be reversed.
    """
    n = len(df)
    lat = df.latitude.to_numpy(dtype=float).copy()
    lon = df.longitude.to_numpy(dtype=float).copy()
    extrap = df.extrap.to_numpy()
    age = np.clip(df.hdr.to_numpy(dtype=float) - df.ts.to_numpy(dtype=float) - AGE_OFFSET_S,
                  0.0, None)
    # Integer codes throughout: object arrays of 6M ids cost GBs per worker.
    trip_code, trip_names = pd.factorize(df.trip_id)
    trip_names = np.asarray(trip_names, dtype=object)
    veh_code = pd.factorize(df.vehicle_id)[0]
    grp = veh_code.astype(np.int64) * len(trip_names) + trip_code
    d_out = np.full(n, np.nan)
    slen = np.full(n, np.nan)
    skey = np.full(n, -1, dtype=np.int32)       # index into shape_keys
    shape_keys: list[tuple[str | None, str]] = []
    key_ix: dict[tuple[str | None, str], int] = {}
    done = np.zeros(n, dtype=bool)
    shift = np.full(n, np.nan)
    for era in _eras_near(city, date_iso):
        todo = np.flatnonzero(~done)
        if not len(todo):
            break
        tt = _timetable(city, era)
        k = tt.index.get_indexer(trip_names)[trip_code[todo]]
        todo, k = todo[k >= 0], k[k >= 0]
        if not len(todo):
            continue
        shp = tt.shape[k]
        d_pub, perp, sl = _project(city, tt, shp, grp[todo], lat[todo], lon[todo])
        ok = perp <= PERP_OK_M
        todo, k, shp, d_pub, sl = todo[ok], k[ok], shp[ok], d_pub[ok], sl[ok]
        d_new = d_pub.copy()
        ex = extrap[todo]
        if ex.any():
            d_new[ex] = reverse_positions(tt, k[ex], d_pub[ex], age[todo][ex])
            la, lo_ = _latlon_at(city, tt, shp[ex], d_new[ex])
            lat[todo[ex]], lon[todo[ex]] = la, lo_
            shift[todo[ex]] = d_pub[ex] - d_new[ex]
        d_out[todo], slen[todo] = d_new, sl
        sc, su = pd.factorize(shp)
        ids = np.array([key_ix.setdefault((era, s_), len(key_ix)) for s_ in su],
                       dtype=np.int32)
        shape_keys = [None] * len(key_ix)
        for kk, vv in key_ix.items():
            shape_keys[vv] = kk
        skey[todo] = ids[sc]
        done[todo] = True
    keep = done | ~extrap
    sh = shift[np.isfinite(shift)]
    stats = {
        "n_extrap": int(extrap.sum()),
        "n_extrap_unreversed_dropped": int((extrap & ~done).sum()),
        "n_observed": int((~extrap).sum()),
        "n_observed_no_trip": int((~extrap & ~done).sum()),
        "shift_m_median": round(float(np.median(sh)), 1) if len(sh) else None,
        "age_s_median": round(float(np.median(age[extrap])), 1) if extrap.any() else None,
    }
    out = df.assign(latitude=lat, longitude=lon, d_m=d_out, shape_len_m=slen,
                    shape_key=skey)[keep].reset_index(drop=True)
    out.attrs["shape_keys"] = shape_keys
    return out, stats


# --------------------------------------------------------------------------
# Vehicle runs
# --------------------------------------------------------------------------

def assign_runs(city: CityConfig, df: pd.DataFrame) -> np.ndarray:
    """Run number per row (rows sorted by vehicle_id, ts).

    A run is one vehicle on one route and direction with no gap over GAP_S.
    Within that, a feed trip_id change is a new trip only if the bus had
    reached the end of the old trip, sits at the start of the new one, or
    has moved backwards by more than BACK_M measured on the new trip's shape.
    """
    veh = pd.factorize(df.vehicle_id)[0]
    route = pd.factorize(df.route_id)[0]
    dirn = df.direction_id.to_numpy()
    trip = pd.factorize(df.trip_id)[0]
    ts = df.ts.to_numpy(dtype=float)
    d = df.d_m.to_numpy()
    slen = df.shape_len_m.to_numpy()
    same_veh = np.r_[False, veh[1:] == veh[:-1]]
    new = ~same_veh
    new[1:] |= (route[1:] != route[:-1]) | (dirn[1:] != dirn[:-1]) | (np.diff(ts) > GAP_S)
    flip = np.r_[False, trip[1:] != trip[:-1]] & ~new
    with np.errstate(invalid="ignore"):
        prev_done = np.r_[False, d[:-1] >= slen[:-1] - TERM_M]
        at_start = d <= TERM_M
    new |= flip & (prev_done | at_start)
    # Backward movement at a flip: place the previous ping on this row's shape.
    cand = np.flatnonzero(flip & ~new)
    if len(cand):
        skey = df.shape_key.to_numpy()
        shape_keys = df.attrs.get("shape_keys", [])
        lat = df.latitude.to_numpy()
        lon = df.longitude.to_numpy()
        cand = cand[skey[cand] >= 0]
        back = np.zeros(len(cand), dtype=bool)
        keys = skey[cand]
        for key, loc in pd.Series(keys).groupby(keys, sort=False).indices.items():
            era, sid = shape_keys[key]
            m = _matcher(_era_zip(city, era), sid, city.max_perp_m)
            if m is None:
                continue
            rows = cand[loc]
            r = m.match(lat[rows - 1], lon[rows - 1], exact_far=False, sequential=False)
            back[loc] = r.on_route & (r.dist_along_m - d[rows] > BACK_M)
        new[cand[back]] = True
    return np.cumsum(new)


# --------------------------------------------------------------------------
# One service date
# --------------------------------------------------------------------------

def _read_raw(city: CityConfig, date_iso: str) -> pd.DataFrame:
    cache = city.resolve(city.archive_cache_dir)
    lo_local = pd.Timestamp(f"{date_iso} {city.service_day_cutover_h:02d}:00", tz=city.tz)
    lo = lo_local.tz_convert("UTC") - pd.Timedelta(hours=PAD_BEFORE_H)
    hi = (lo_local + pd.Timedelta(days=1)).tz_convert("UTC") + pd.Timedelta(hours=PAD_AFTER_H)
    frames = []
    for rank, (agency, kind) in enumerate(city.r2_sources):
        # observed sources sort first so they win the first-seen dedupe
        rank = 0 if kind != EXTRAPOLATED else 1 + rank
        cur = lo.floor("h")
        while cur <= hi:
            p = cache / (f"agency={agency}__year={cur.year:04d}__month={cur.month:02d}__"
                         f"day={cur.day:02d}__hour={cur.hour:02d}.parquet")
            cur += pd.Timedelta(hours=1)
            if not (p.exists() and p.stat().st_size > 0):
                continue
            try:
                pf = pq.ParquetFile(p)
                cols = [c for c in RAW_COLUMNS if c in pf.schema_arrow.names]
                t = pf.read(columns=cols).to_pandas(
                    types_mapper={pa.string(): pd.StringDtype("pyarrow"),
                                  pa.large_string(): pd.StringDtype("pyarrow")}.get)
            except Exception as e:  # noqa: BLE001 — a bad hour must not kill the date
                print(f"unreadable hour-file {p.name}: {e}", file=sys.stderr, flush=True)
                continue
            t = t.dropna(subset=["timestamp", "trip_id", "vehicle_id", "route_id",
                                 "latitude", "longitude"])
            # SIRI ProgressStatus prevTrip: the bus is still on the block's
            # previous trip, so the position does not belong to this trip_id.
            if "vehicle_status" in t:
                prev = t["vehicle_status"].fillna("").str.contains("PREV_TRIP")
                t = t[~prev.to_numpy(dtype=bool)].drop(columns="vehicle_status")
            # first-seen per (vehicle, timestamp) within the hour already:
            # halves the day's frame before the cross-hour dedupe
            if "feed_timestamp" in t:
                t = t.sort_values("feed_timestamp", kind="stable")
            t = t.drop_duplicates(["vehicle_id", "timestamp"])
            t["source"] = agency
            t["extrap"] = kind == EXTRAPOLATED
            t["rank"] = np.int8(rank)
            frames.append(t)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df = df.dropna(subset=["timestamp", "trip_id", "vehicle_id", "route_id",
                           "latitude", "longitude"])
    ts = pd.to_datetime(df["timestamp"], utc=True)
    df = df[(ts >= lo).to_numpy() & (ts <= hi).to_numpy()].copy()
    df["ts"] = pd.to_datetime(df["timestamp"], utc=True).astype("int64") // 10**(
        9 if str(df["timestamp"].dtype).startswith("datetime64[ns") else 3)
    hdr = pd.to_datetime(df["feed_timestamp"], utc=True) if "feed_timestamp" in df else None
    df["hdr"] = (df["ts"] if hdr is None else
                 (hdr.astype("int64") // 10**(9 if str(hdr.dtype).startswith("datetime64[ns") else 3)
                  ).where(hdr.notna(), df["ts"]))
    if "direction_id" not in df:
        df["direction_id"] = -1
    df["direction_id"] = df["direction_id"].fillna(-1).astype(np.int8)
    if city.deadhead_route_ids:
        df = df[~df.route_id.isin(city.deadhead_route_ids)]
    return df


def ingest_date(args: tuple[str, str, bool]) -> dict:
    city_id, date_iso, force = args
    try:
        return _ingest_date(city_id, date_iso, force)
    except Exception as e:  # noqa: BLE001
        import traceback
        return {"date": date_iso, "error": f"{type(e).__name__}: {e}",
                "trace": traceback.format_exc(limit=6)}


def _ingest_date(city_id: str, date_iso: str, force: bool) -> dict:
    city = get_city(city_id)
    out = city.resolve(city.ping_dir) / f"date={date_iso}.parquet"
    if out.exists() and not force:
        return {"date": date_iso, "note": "exists"}
    t0 = time.time()
    df = _read_raw(city, date_iso)
    if df.empty:
        return {"date": date_iso, "note": "no_pings"}
    n_raw = len(df)
    df = (df.sort_values(["vehicle_id", "ts", "rank", "hdr"], kind="stable")
            .drop_duplicates(["vehicle_id", "ts"]).reset_index(drop=True))
    n_first = len(df)
    df, stats = correct(city, df, date_iso)
    run = assign_runs(city, df)
    local = pd.to_datetime(df.ts, unit="s", utc=True).dt.tz_convert(city.tz)
    sdate = (local - pd.Timedelta(hours=city.service_day_cutover_h)).dt.date
    want = pd.Timestamp(date_iso).date()
    sel = (sdate == want).to_numpy()
    df, run = df[sel].reset_index(drop=True), run[sel]
    df = df.drop(columns=["d_m", "shape_len_m", "shape_key", "hdr", "rank", "extrap"],
                 errors="ignore")
    if df.empty:
        return {"date": date_iso, "note": "no_pings"}
    # Run id: first feed trip_id of the run, "~k" when the vehicle repeats it.
    first = np.r_[True, run[1:] != run[:-1]]
    heads = df.loc[first, ["vehicle_id", "trip_id"]].astype(str)
    k = heads.groupby(["vehicle_id", "trip_id"]).cumcount().to_numpy() + 1
    names = np.where(k == 1, heads.trip_id.to_numpy(),
                     heads.trip_id.to_numpy() + "~" + k.astype(str))
    run_name = names[np.cumsum(first) - 1]
    n_flip_runs = int(pd.Series(df.trip_id.astype(str).to_numpy())
                      .groupby(run).nunique().gt(1).sum())
    table = pa.table({
        "timestamp": pa.array(df.ts.to_numpy() * 1000, pa.timestamp("ms", tz="UTC")),
        "trip_id": pa.array(run_name, pa.string()),
        "feed_trip_id": pa.array(df.trip_id.astype(str).to_numpy(), pa.string()),
        "route_id": pa.array(df.route_id.astype(str).to_numpy()).dictionary_encode(),
        "direction_id": pa.array(df.direction_id.to_numpy(), pa.int8()),
        "vehicle_id": pa.array(df.vehicle_id.astype(str).to_numpy()).dictionary_encode(),
        "latitude": pa.array(np.round(df.latitude.to_numpy(), 6)),
        "longitude": pa.array(np.round(df.longitude.to_numpy(), 6)),
        "source": pa.array(df.source.astype(str).to_numpy()).dictionary_encode(),
        "service_date": pa.array(np.full(len(df), want), pa.date32()),
    }).cast(OUT_SCHEMA)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".parquet.tmp")
    pq.write_table(table, tmp, compression="zstd")
    os.replace(tmp, out)
    return {"date": date_iso, "n_raw": n_raw, "n_first_seen": n_first,
            "n_out": len(df), "n_runs": int(first.sum()),
            "n_feed_trip_groups": int(df.groupby(["vehicle_id", "trip_id"]).ngroups),
            "n_runs_multi_trip_id": n_flip_runs, **stats,
            "wall_s": round(time.time() - t0, 1)}


def raw_dates(city: CityConfig) -> list[str]:
    """Service dates covered by the cached raw hour-files of every source."""
    cache = city.resolve(city.archive_cache_dir)
    hours = []
    for agency in city.r2_agencies:
        for p in cache.glob(f"agency={agency}__*.parquet"):
            try:
                kv = dict(part.split("=") for part in p.stem.split("__"))
                hours.append(pd.Timestamp(
                    f"{kv['year']}-{kv['month']}-{kv['day']} {kv['hour']}:00", tz="UTC"))
            except (ValueError, KeyError):
                continue
    if not hours:
        return []
    cut = pd.Timedelta(hours=city.service_day_cutover_h)
    d0 = (min(hours).tz_convert(city.tz) - cut).date()
    d1 = (max(hours).tz_convert(city.tz) - cut).date()
    return [str(d.date()) for d in pd.date_range(d0, d1, freq="D")]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--city", required=True)
    ap.add_argument("--date", default=None)
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    city = get_city(args.city)
    if not (city.r2_sources and city.ping_dir):
        print(f"{city.city_id}: no r2_sources/ping_dir configured; nothing to ingest")
        return
    dates = [args.date] if args.date else raw_dates(city)
    if args.start:
        dates = [d for d in dates if d >= args.start]
    if args.end:
        dates = [d for d in dates if d <= args.end]
    if not dates:
        raise SystemExit("no service dates in the raw cache (run prefetch first?)")
    eras = sorted({gtfs_history.era_id(city, d) for d in dates} - {None}) or [None]
    for era in gtfs_history.all_eras(city) or eras:
        build_timetable(city, era)
    print(f"{len(dates)} service date(s): {dates[0]} .. {dates[-1]}", flush=True)
    index = city.resolve(city.ping_dir) / "ingest_index.jsonl"
    index.parent.mkdir(parents=True, exist_ok=True)
    work = [(args.city, d, args.force) for d in dates]
    t0 = time.time()
    n_err = 0

    def log(i: int, s: dict) -> None:
        nonlocal n_err
        with open(index, "a") as fh:
            fh.write(json.dumps(s) + "\n")
        n_err += bool(s.get("error"))
        msg = s.get("error") or s.get("note") or (
            f"{s['n_out']:,} pings, {s['n_runs']:,} runs, "
            f"dropped {s['n_extrap_unreversed_dropped']:,} unreversible, "
            f"shift {s['shift_m_median']} m")
        print(f"[{i}/{len(dates)}] {s['date']}: {msg} ({time.time() - t0:.0f}s elapsed)",
              flush=True)

    if args.workers <= 1:
        for i, w in enumerate(work, 1):
            log(i, ingest_date(w))
    else:
        with Pool(args.workers) as pool:
            for i, s in enumerate(pool.imap_unordered(ingest_date, work), 1):
                log(i, s)
    print("done" if not n_err else f"done with {n_err} error(s)")
    sys.exit(1 if n_err else 0)


if __name__ == "__main__":
    main()
