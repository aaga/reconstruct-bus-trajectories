"""Network-wide batch reconstruction → per-traversal segment times.

For every trip on a service date (all routes): geometric shape assignment
(``assign_shapes``), full LOCREG-PCHIP reconstruction, then vectorized
segment-boundary crossing times (Eq 3.3 "last time at x" convention). One
traversal row per (trip, segment) lands in a per-(service_date, route)
parquet checkpoint:

    outputs/network/<city>/traversals/service_date=YYYY-MM-DD/route=<id>.parquet

Checkpoints are atomic (tmp + os.replace) and the driver skips existing files,
so the full-archive run is resumable. Per-unit stats append to
``traversals_index.jsonl``.

Usage:
    # one service date (pilot):
    PYTHONPATH=src uv run python analysis/network/run_reconstruct.py --city cta --date 2026-05-05
    # a range, parallel:
    PYTHONPATH=src uv run python analysis/network/run_reconstruct.py --city cta \
        --start 2026-04-28 --end 2026-07-20 --workers 8
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from analysis.network.assign_shapes import (  # noqa: E402
    LOW_CONFIDENCE_SCORE,
    Assignment,
    choose_shape,
    monotone_frac,
)
from core.decompose.travel_time import last_times_at_boundaries  # noqa: E402
from core.mapmatch.shape_snap import SnapToShapeMatcher  # noqa: E402
from core.smooth import locreg_pchip  # noqa: E402
from dataio.cities import CityConfig, get_city  # noqa: E402
from dataio.gtfs import load_gtfs_shape_with_dist  # noqa: E402

# Trip gates (deliberately looser than build_all_sb_trips: partial trips still
# yield valid traversals for the segments they fully cover).
MIN_PINGS = 10
MAX_DURATION_H = 4.0
MIN_MONOTONE = 0.9
TERMINAL_M = 150.0  # truncate after first arrival within this of shape end
EDGE_MARGIN_M = 50.0  # segment must sit this far inside the observed d-range

FLAG_TOUCHED_TERMINAL = 1 << 0
FLAG_LOW_CONFIDENCE = 1 << 1

TRAVERSAL_SCHEMA = pa.schema(
    [
        ("seg_id", pa.dictionary(pa.int32(), pa.string())),
        ("route_id", pa.dictionary(pa.int32(), pa.string())),
        ("shape_id", pa.dictionary(pa.int32(), pa.string())),
        ("direction", pa.dictionary(pa.int32(), pa.string())),
        ("service_date", pa.date32()),
        ("trip_key", pa.string()),
        ("vehicle_id", pa.dictionary(pa.int32(), pa.string())),
        ("t_enter_utc", pa.timestamp("ms", tz="UTC")),
        ("t_exit_utc", pa.timestamp("ms", tz="UTC")),
        ("t_obs_s", pa.float32()),
        ("seg_len_m", pa.float32()),
        ("n_pings_in_seg", pa.uint8()),
        ("max_gap_in_seg_s", pa.float32()),
        ("hour_local", pa.uint8()),
        ("period", pa.dictionary(pa.int32(), pa.string())),
        ("flags", pa.uint8()),
    ]
)


# --------------------------------------------------------------------------
# Worker-local caches (built lazily per process)
# --------------------------------------------------------------------------

_G: dict = {}


def _init_worker(city_id: str) -> None:
    city = get_city(city_id)
    reg_path = REPO / "outputs" / "network" / city.city_id / "segment_registry.json"
    reg = json.loads(reg_path.read_text())
    _G["city"] = city
    _G["registry_meta"] = reg["meta"]
    _G["shapes"] = reg["shapes"]  # shape_id -> {route_id, direction, seg_bounds}
    # route_id -> [shape_id, ...]
    by_route: dict[str, list[str]] = {}
    for sid, rec in reg["shapes"].items():
        by_route.setdefault(rec["route_id"], []).append(sid)
    _G["shapes_by_route"] = by_route
    _G["matchers"] = {}  # shape_id -> (matcher, shape_len_m)


def _matcher(shape_id: str) -> tuple[SnapToShapeMatcher, float]:
    if shape_id not in _G["matchers"]:
        city: CityConfig = _G["city"]
        polyline, dist_m = load_gtfs_shape_with_dist(
            city.resolve(city.gtfs_zip), shape_id
        )
        m = SnapToShapeMatcher(
            polyline, max_perp_m=city.max_perp_m, dist_along_m_per_vertex=dist_m
        )
        length = float(dist_m[-1]) if dist_m is not None else float(m._cum_at_vert[-1])
        _G["matchers"][shape_id] = (m, length)
    return _G["matchers"][shape_id]


# --------------------------------------------------------------------------
# Hour-file loading for one service date
# --------------------------------------------------------------------------

def _service_date_pings(city: CityConfig, date_iso: str) -> pd.DataFrame:
    """All pings whose Chicago service date == date_iso (03:00 cutover).

    Loads the UTC hour-files spanning [date 03:00, date+1 03:00] local with
    1 h pad on both sides, from the local cache only (run prefetch first).
    """
    cache_dir = city.resolve(city.archive_cache_dir)
    lo_local = pd.Timestamp(f"{date_iso} 0{city.service_day_cutover_h}:00", tz=city.tz)
    hi_local = lo_local + pd.Timedelta(days=1)
    lo = lo_local.tz_convert("UTC") - pd.Timedelta(hours=1)
    hi = hi_local.tz_convert("UTC") + pd.Timedelta(hours=1)

    frames = []
    cur = lo.floor("h")
    while cur <= hi:
        path = (
            f"agency={city.r2_agency}/year={cur.year:04d}/month={cur.month:02d}/"
            f"day={cur.day:02d}/hour={cur.hour:02d}.parquet"
        )
        local = cache_dir / path.replace("/", "__")
        if local.exists() and local.stat().st_size > 0:
            try:
                frames.append(pq.ParquetFile(local).read().to_pandas())
            except Exception:
                # Corrupt cache entry (truncated download / error-page body).
                # Delete, refetch once, retry; a still-bad hour is skipped
                # rather than killing an 8-worker batch.
                log_msg = f"corrupt hour-file {local.name}; refetching"
                print(log_msg, file=sys.stderr, flush=True)
                local.unlink(missing_ok=True)
                try:
                    from dataio.realtime import ARCHIVE_URL, fetch

                    fetch(f"{ARCHIVE_URL}/{path}", local)
                    frames.append(pq.ParquetFile(local).read().to_pandas())
                except Exception as e:  # noqa: BLE001
                    print(f"  refetch failed, skipping hour: {e}", file=sys.stderr, flush=True)
                    local.unlink(missing_ok=True)
        cur += pd.Timedelta(hours=1)
    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames, ignore_index=True)
    df = df[~df.route_id.isin(city.deadhead_route_ids)].copy()
    ts = pd.to_datetime(df["timestamp"], utc=True)
    df["ts_utc"] = ts
    # Service date: local time shifted back by the cutover hour, then date.
    local = ts.dt.tz_convert(city.tz)
    df["service_date"] = (local - pd.Timedelta(hours=city.service_day_cutover_h)).dt.date
    return df[df.service_date == pd.Timestamp(date_iso).date()]


# --------------------------------------------------------------------------
# Per-trip processing
# --------------------------------------------------------------------------

def _process_trip(
    trip: pd.DataFrame, date_iso: str, rejects: Counter
) -> list[dict] | None:
    city: CityConfig = _G["city"]
    # The scraper polls faster than the feed updates positions — consecutive
    # snapshots repeat the same reported timestamp. Keep one row per instant.
    trip = trip.sort_values("ts_utc").drop_duplicates(subset="ts_utc")
    if len(trip) < MIN_PINGS:
        rejects["few_pings"] += 1
        return None
    t0 = trip["ts_utc"].iloc[0]
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
    matchers = {sid: _matcher(sid)[0] for sid in candidates}
    lens = {sid: _matcher(sid)[1] for sid in candidates}

    got = choose_shape(lats, lons, matchers, lens)
    if isinstance(got, str):
        rejects[got] += 1
        return None
    asg: Assignment = got

    on = asg.match.on_route
    d_all = asg.match.dist_along_m
    t_on = t_sec_all[on]
    d_on = d_all[on]

    if monotone_frac(d_on) < MIN_MONOTONE:
        rejects["not_monotone"] += 1
        return None

    # Truncate at first terminal arrival (kills layover tails).
    shape_len = lens[asg.shape_id]
    touched_terminal = False
    at_term = np.nonzero(d_on >= shape_len - TERMINAL_M)[0]
    if len(at_term):
        touched_terminal = True
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

    d_min, d_max = float(sm.x.min()), float(sm.x.max())
    bounds = _G["shapes"][asg.shape_id]["seg_bounds"]
    keep = [
        b for b in bounds
        if b[1] >= d_min + EDGE_MARGIN_M and b[2] <= d_max - EDGE_MARGIN_M
    ]
    if not keep:
        rejects["no_covered_segments"] += 1
        return None

    xs = np.array(sorted({b[1] for b in keep} | {b[2] for b in keep}))
    t_at = dict(zip(xs, last_times_at_boundaries(f, xs)))

    shape_rec = _G["shapes"][asg.shape_id]
    flags_base = FLAG_LOW_CONFIDENCE if asg.score < LOW_CONFIDENCE_SCORE else 0
    trip_key = (
        f"{trip['trip_id'].iloc[0]}_{trip['vehicle_id'].iloc[0]}_{date_iso}"
    )
    tz = city.tz

    d_on_sorted = np.sort(d_on)
    rows: list[dict] = []
    for seg_id, x_lo, x_hi in keep:
        te, tx = t_at[x_lo], t_at[x_hi]
        t_obs = tx - te
        if t_obs <= 0:
            continue
        enter_abs = t0 + pd.Timedelta(seconds=float(te))
        exit_abs = t0 + pd.Timedelta(seconds=float(tx))
        # Pings spatially inside the segment; time gaps incl. virtual edges.
        i_lo, i_hi = np.searchsorted(d_on_sorted, [x_lo, x_hi])
        n_in = int(i_hi - i_lo)
        in_t = t_on[(d_on >= x_lo) & (d_on <= x_hi)]
        edges = np.concatenate([[te], np.sort(in_t), [tx]]) if n_in else np.array([te, tx])
        max_gap = float(np.diff(edges).max()) if len(edges) > 1 else 0.0

        enter_local = enter_abs.tz_convert(tz)
        flags = flags_base
        if touched_terminal and x_hi >= shape_len - TERMINAL_M - EDGE_MARGIN_M:
            flags |= FLAG_TOUCHED_TERMINAL
        rows.append(
            {
                "seg_id": seg_id,
                "route_id": route_id,
                "shape_id": asg.shape_id,
                "direction": shape_rec["direction"],
                "service_date": pd.Timestamp(date_iso).date(),
                "trip_key": trip_key,
                "vehicle_id": str(trip["vehicle_id"].iloc[0]),
                "t_enter_utc": enter_abs,
                "t_exit_utc": exit_abs,
                "t_obs_s": float(t_obs),
                "seg_len_m": float(x_hi - x_lo),
                "n_pings_in_seg": min(n_in, 255),
                "max_gap_in_seg_s": max_gap,
                "hour_local": int(enter_local.hour),
                "period": _G["city"].period_for_hour(int(enter_local.hour)),
                "flags": flags,
            }
        )
    if not rows:
        rejects["no_valid_traversals"] += 1
        return None
    return rows


# --------------------------------------------------------------------------
# Per-(date, route) unit
# --------------------------------------------------------------------------

def _out_dir(city: CityConfig) -> Path:
    return REPO / "outputs" / "network" / city.city_id / "traversals"


def process_date(args: tuple[str, str, bool] | tuple[str, str, bool, str | None]) -> list[dict]:
    """Process one service date (all routes). Returns per-route stat dicts.

    Never raises: any per-date failure is reported as an ``error`` stat so one
    bad date can't kill the whole pool (resume + ``--force`` redo it later).
    """
    try:
        return _process_date_inner(args)
    except Exception as e:  # noqa: BLE001
        import traceback

        return [{
            "date": args[1],
            "route": None,
            "n_trips_kept": 0,
            "error": f"{type(e).__name__}: {e}",
            "trace": traceback.format_exc(limit=6),
        }]


def _process_date_inner(args) -> list[dict]:
    city_id, date_iso, force, *rest = args
    only_route = rest[0] if rest else None
    if "city" not in _G:
        _init_worker(city_id)
    city: CityConfig = _G["city"]

    date_dir = _out_dir(city) / f"service_date={date_iso}"
    stats: list[dict] = []

    df = _service_date_pings(city, date_iso)
    if df.empty:
        return [{"date": date_iso, "route": None, "n_trips_kept": 0, "note": "no_pings"}]
    if only_route is not None:
        df = df[df.route_id == only_route]

    for route_id, route_df in df.groupby("route_id", sort=True):
        out_path = date_dir / f"route={route_id}.parquet"
        if out_path.exists() and not force:
            continue
        t0 = time.time()
        rejects: Counter = Counter()
        all_rows: list[dict] = []
        n_kept = 0
        for _, trip in route_df.groupby(["trip_id", "vehicle_id"], sort=False):
            rows = _process_trip(trip, date_iso, rejects)
            if rows:
                all_rows.extend(rows)
                n_kept += 1

        out_path.parent.mkdir(parents=True, exist_ok=True)
        table = (
            pa.Table.from_pylist(all_rows, schema=TRAVERSAL_SCHEMA)
            if all_rows
            else TRAVERSAL_SCHEMA.empty_table()
        )
        tmp = out_path.with_suffix(".parquet.tmp")
        pq.write_table(table, tmp, compression="zstd")
        os.replace(tmp, out_path)

        stats.append(
            {
                "date": date_iso,
                "route": str(route_id),
                "n_trips_seen": int(route_df.groupby(["trip_id", "vehicle_id"]).ngroups),
                "n_trips_kept": n_kept,
                "n_traversals": len(all_rows),
                "rejects": dict(rejects),
                "wall_s": round(time.time() - t0, 2),
            }
        )
    return stats


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def _dates_in_archive(city: CityConfig) -> list[str]:
    """Service dates covered by the local hour-file cache."""
    cache_dir = city.resolve(city.archive_cache_dir)
    hours = []
    for p in cache_dir.glob(f"agency={city.r2_agency}__*.parquet"):
        try:
            kv = dict(part.split("=") for part in p.stem.split("__"))
            hours.append(pd.Timestamp(
                f"{kv['year']}-{kv['month']}-{kv['day']} {kv['hour']}:00", tz="UTC"
            ))
        except (ValueError, KeyError):
            continue
    if not hours:
        return []
    lo, hi = min(hours), max(hours)
    cut = pd.Timedelta(hours=city.service_day_cutover_h)
    d0 = (lo.tz_convert(city.tz) - cut).date()
    d1 = (hi.tz_convert(city.tz) - cut).date()
    return [str(d.date()) for d in pd.date_range(d0, d1, freq="D")]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--city", default="cta")
    ap.add_argument("--date", default=None, help="one service date YYYY-MM-DD")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--workers", type=int, default=1)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--route", default=None, help="debug: process only this route")
    args = ap.parse_args()

    city = get_city(args.city)
    if args.date:
        dates = [args.date]
    else:
        dates = _dates_in_archive(city)
        if args.start:
            dates = [d for d in dates if d >= args.start]
        if args.end:
            dates = [d for d in dates if d <= args.end]
    if not dates:
        raise SystemExit("no service dates to process (run prefetch first?)")
    print(f"{len(dates)} service date(s): {dates[0]} .. {dates[-1]}")

    index_path = _out_dir(city) / "traversals_index.jsonl"
    index_path.parent.mkdir(parents=True, exist_ok=True)

    work = [(args.city, d, args.force, args.route) for d in dates]
    t_start = time.time()
    done = 0

    def _log(stats: list[dict]) -> None:
        nonlocal done
        done += 1
        with open(index_path, "a") as fh:
            for s in stats:
                fh.write(json.dumps(s) + "\n")
        kept = sum(s.get("n_trips_kept", 0) for s in stats)
        trav = sum(s.get("n_traversals", 0) for s in stats)
        date = stats[0]["date"] if stats else "?"
        errs = [s for s in stats if s.get("error")]
        suffix = f"  !! {len(errs)} ERROR(S): {errs[0]['error']}" if errs else ""
        print(
            f"[{done}/{len(dates)}] {date}: {kept} trips, {trav} traversals "
            f"({time.time() - t_start:.0f}s elapsed){suffix}",
            flush=True,
        )

    if args.workers <= 1:
        _init_worker(args.city)
        for w in work:
            _log(process_date(w))
    else:
        with Pool(args.workers, initializer=_init_worker, initargs=(args.city,)) as pool:
            for stats in pool.imap_unordered(process_date, work):
                _log(stats)

    print("done")


if __name__ == "__main__":
    main()
