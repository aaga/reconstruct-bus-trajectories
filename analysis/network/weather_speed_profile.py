"""Per-10 ft average speed in heavy-rain and in snow hours, per segment.

Companion to weather_effect.py for the segment view's speed overlay. The
overall overlay (dist/<sid>.json "ping_v") comes from delay_events'
traj_speed DAILY sums, which can't be split by hour, so the condition curves
re-reconstruct just the trips with a heavy-rain / snow traversal:

  1. tag traversals exactly like weather_effect (hour containing the
     traversal midpoint; snow = airport METAR falling snow, city-wide;
     heavy rain = MRMS >= 7.6 mm in the segment-midpoint cell; snow wins);
  2. per service date (parallel), load that date's pings (only the needed
     routes), reuse each trip's stored shape assignment, and run
     delay_events._process_trip with traj_speed on;
  3. keep the 10 ft crossing times of each trip's TAGGED segments, summed
     per (segment, bucket, condition).

Speed per bucket = bucket length / mean crossing time (the overlay's
estimator; last bucket at its true length); buckets with fewer than
MIN_BUCKET crossings stay null.

Output: dashboard/data/network[/<city>]/wx_speed/<sid>.json
        {"bucket_ft": 10, "heavy_rain": {"v": [m/s|null], "n": [...]},
         "snow": {...}}   (a condition is omitted when it has no data)

Usage:
    PYTHONPATH=src uv run python analysis/network/weather_speed_profile.py --city cta --workers 6
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from collections import Counter, defaultdict
from multiprocessing import Pool
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from analysis.network.weather_effect import HEAVY_MM, midpoint, snow_hours  # noqa: E402
from dataio.cities import get_city  # noqa: E402

BUCKET_M = 10.0 / 3.28084
MIN_BUCKET = 20
MAX_GAP_S = 180.0   # = build_payloads / weather_effect
FLAG_TOUCHED_TERMINAL = 1
CONDS = ("heavy_rain", "snow")

_W: dict = {}


def _tag(city, reg, days) -> pd.DataFrame:
    """(service_date, trip_key, route_id, seg_id, cond) for heavy/snow traversals."""
    base = REPO / "outputs" / "network" / city.city_id
    rain = np.load(REPO / "caches" / "rain" / city.city_id / "mrms_qpe01h_pass2.npz")
    lat, lon, valid, mm = rain["lat"], rain["lon"], rain["valid_utc"], rain["mm"]
    rows = []
    for s, rec in reg["segments"].items():
        g = rec.get("geometry_lonlat")
        if g:
            x, y = midpoint(g)
            rows.append((s, int(np.argmin(np.abs(lat - y))), int(np.argmin(np.abs(lon - x)))))
    cells = sorted({(iy, ix) for _, iy, ix in rows})
    cid = {c: i for i, c in enumerate(cells)}
    seg_cell = pd.DataFrame({"seg_id": [r[0] for r in rows],
                             "cell": [cid[(r[1], r[2])] for r in rows]})
    flat = np.stack([mm[:, iy, ix] for iy, ix in cells], axis=1)
    hi, ci = np.nonzero(np.nan_to_num(flat) >= HEAVY_MM)
    heavy = pd.DataFrame({"cell": ci.astype(np.int32), "he": valid[hi]})
    snow = pd.DataFrame({"he": snow_hours(city, min(days), max(days))})
    con = duckdb.connect()
    con.register("seg_cell", seg_cell)
    con.register("heavy", heavy)
    con.register("snow", snow)
    return con.execute(f"""
        WITH t AS (
          SELECT CAST(service_date AS VARCHAR) AS d, trip_key, route_id, seg_id,
                 ((floor(((epoch(t_enter_utc) + epoch(t_exit_utc)) / 2) / 3600) + 1) * 3600)::BIGINT AS he
          FROM read_parquet('{base}/traversals/service_date=*/route=*.parquet', hive_partitioning=true)
          WHERE t_obs_s > 0 AND max_gap_in_seg_s <= {MAX_GAP_S}
            AND (flags & {FLAG_TOUCHED_TERMINAL}) = 0)
        SELECT t.d, t.trip_key, CAST(t.route_id AS VARCHAR) AS route_id, t.seg_id,
               CASE WHEN s.he IS NOT NULL THEN 'snow' ELSE 'heavy_rain' END AS cond
        FROM t
        JOIN seg_cell sc USING (seg_id)
        LEFT JOIN snow s ON s.he = t.he
        LEFT JOIN heavy h ON h.cell = sc.cell AND h.he = t.he
        WHERE s.he IS NOT NULL OR h.he IS NOT NULL
    """).df()


def _init(city_id: str) -> None:
    from analysis.network import delay_events as de
    de._init_worker_ev(city_id, 5.0, "", True, False)
    _W["de"] = de
    _W["city"] = get_city(city_id)


def _do_date(args) -> dict:
    """One service date -> {(seg, bucket, cond): [n, sum_dt]}."""
    d, want = args            # want: {trip_key: {seg_id: cond}}
    de, city = _W["de"], _W["city"]
    out: dict = defaultdict(lambda: [0, 0.0])
    try:
        de._set_era(d)
        df = de._service_date_pings(city, d)
        if df.empty:
            return {}
        keys = (df["trip_id"].astype(str) + "_" + df["vehicle_id"].astype(str) + "_" + d)
        df = df[keys.isin(want.keys())]
        doors, door_stops = (de._door_intervals(city, d) if city.has_door_data else ({}, {}))
        assigned = de._stored_assignments(city, d)
        for (tid, veh), trip in df.groupby(["trip_id", "vehicle_id"], sort=False):
            segs = want.get(f"{tid}_{veh}_{d}")
            if not segs:
                continue
            got = de._process_trip(trip, d, doors, Counter(), assigned, door_stops)
            if got is None:
                continue
            for s_, b_, dt_ in got[2]:
                c = segs.get(s_)
                if c:
                    a = out[(s_, int(b_), c)]
                    a[0] += 1
                    a[1] += float(dt_)
    except Exception as e:  # noqa: BLE001 — one bad date must not sink the run
        print(f"  {d}: failed ({e})", flush=True)
    return dict(out)


def build(city_id: str, workers: int) -> None:
    city = get_city(city_id)
    base = REPO / "outputs" / "network" / city.city_id
    reg = json.loads((base / "segment_registry.json").read_text())
    days = json.loads((base / "date_attrs.json").read_text())["days"]
    t0 = time.time()
    tags = _tag(city, reg, days)
    print(f"{city.city_id}: {len(tags):,} tagged traversals "
          f"({tags.cond.value_counts().to_dict()}) on {tags.d.nunique()} dates "
          f"({time.time() - t0:.0f}s)", flush=True)
    jobs = []
    for d, g in tags.groupby("d"):
        want: dict = defaultdict(dict)
        for tk, s, c in zip(g.trip_key, g.seg_id, g.cond):
            want[tk][s] = c
        jobs.append((d, dict(want)))
    jobs.sort(key=lambda j: -sum(len(v) for v in j[1].values()))  # big dates first

    acc: dict = defaultdict(lambda: [0, 0.0])
    t0 = time.time()
    with Pool(workers, initializer=_init, initargs=(city_id,)) as pool:
        for i, part in enumerate(pool.imap_unordered(_do_date, jobs), 1):
            for k, (n, s) in part.items():
                a = acc[k]
                a[0] += n
                a[1] += s
            if i % 20 == 0 or i == len(jobs):
                print(f"  [{i}/{len(jobs)}] dates ({time.time() - t0:.0f}s)", flush=True)

    seg_index = {s: i for i, s in enumerate(sorted(reg["segments"]))}
    per: dict = defaultdict(lambda: defaultdict(dict))
    for (s, b, c), (n, sdt) in acc.items():
        per[s][c][b] = (n, sdt)
    net = REPO / "dashboard" / "data" / "network"
    out_dir = (net if city.city_id == "cta" else net / city.city_id) / "wx_speed"
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)
    n_files = 0
    for s, conds in per.items():
        L = reg["segments"][s]["len_m"]
        nb = int(np.ceil(L / BUCKET_M))
        payload: dict = {"bucket_ft": 10.0}
        for c, buckets in conds.items():
            v = [None] * nb
            nn = [0] * nb
            for b, (n, sdt) in buckets.items():
                if not (0 <= b < nb) or sdt <= 0:
                    continue
                nn[b] = int(n)
                if n >= MIN_BUCKET:
                    blen = L - (nb - 1) * BUCKET_M if b == nb - 1 else BUCKET_M
                    v[b] = round(blen / (sdt / n), 2)  # m/s, like ping_v
            if any(x is not None for x in v):
                payload[c] = {"v": v, "n": nn}
        if any(c in payload for c in CONDS):
            (out_dir / f"{seg_index[s]}.json").write_text(json.dumps(payload, separators=(",", ":")))
            n_files += 1
    print(f"wrote {n_files:,} segment files -> {out_dir} ({time.time() - t0:.0f}s re-reconstruction)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--city", required=True)
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    build(a.city, a.workers)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
