"""Join door events onto traversals → per-traversal dwell/APC sidecar.

For every legacy traversal row (trip_key, seg_id), sum the vehicle's door
events whose instant falls inside [t_enter, t_exit]:

    door_n   completed door cycles in the window
    dwell_s  Σ door-open seconds ("delay at stops", per user decision:
             door time only — approach/departure stays in-motion)
    ons/offs Σ (ron+fon) / Σ (roff+foff)
    load_sum Σ passenger_load over events (kept under the hood; divide by
             door_n for a mean-load-when-doors-open)

Coverage discipline: a traversal is "door-covered" iff its vehicle reported
ANY door event that service day. Covered traversals with no events in the
window get an explicit zero row — so downstream can distinguish "true zero
dwell" from "vehicle not instrumented / month not delivered". Sidecar rows
exist ONLY for covered traversals.

Timezone: event_time is naive local wall-clock; on first run the tz is
verified empirically (containment of a sample day's events within the same
vehicle's AVL trip windows under Chicago vs UTC interpretation).

Output: outputs/network/<city>/door_sidecar/service_date=YYYY-MM-DD.parquet
Resumable (skips existing dates). Run after door_events.py.

Usage:
    PYTHONPATH=src uv run python analysis/network/door_join.py --city cta
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import duckdb

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from dataio.cities import CityConfig, get_city  # noqa: E402


def _connect(city: CityConfig) -> tuple[duckdb.DuckDBPyConnection, str, str]:
    con = duckdb.connect()
    ev_glob = str(city.resolve("caches/door_events") / city.city_id / "*.parquet")
    trav_glob = str(
        REPO / "outputs" / "network" / city.city_id / "traversals"
        / "service_date=*" / "route=*.parquet"
    )
    return con, ev_glob, trav_glob


def verify_timezone(city: CityConfig, sample_date: str = "2026-06-03") -> dict:
    """Fraction of a day's door events landing inside the same vehicle's
    traversal windows, under Chicago-local vs UTC interpretation."""
    con, ev_glob, trav_glob = _connect(city)
    out = {}
    for label, expr in (
        ("chicago", f"(event_time AT TIME ZONE '{city.tz}')"),
        ("utc", "(event_time AT TIME ZONE 'UTC')"),
    ):
        r = con.execute(
            f"""
            WITH ev AS (
              SELECT bus_id, {expr} AS t_utc
              FROM read_parquet('{ev_glob}')
              WHERE event_time::DATE = DATE '{sample_date}'
            ),
            tr AS (
              SELECT vehicle_id, t_enter_utc, t_exit_utc
              FROM read_parquet('{trav_glob}')
              WHERE service_date BETWEEN DATE '{sample_date}' - 1 AND DATE '{sample_date}' + 1
            )
            SELECT
              (SELECT count(*) FROM ev) AS n_events,
              (SELECT count(*) FROM ev
                WHERE EXISTS (SELECT 1 FROM tr
                              WHERE tr.vehicle_id = ev.bus_id
                                AND ev.t_utc BETWEEN tr.t_enter_utc AND tr.t_exit_utc)
              ) AS n_contained
            """
        ).fetchone()
        out[label] = {"events": r[0], "contained": r[1],
                      "frac": round(r[1] / r[0], 4) if r[0] else 0.0}
    return out


def build_sidecar(city: CityConfig, force: bool = False) -> None:
    con, ev_glob, trav_glob = _connect(city)
    out_dir = REPO / "outputs" / "network" / city.city_id / "door_sidecar"
    out_dir.mkdir(parents=True, exist_ok=True)
    cut = city.service_day_cutover_h

    dates = [
        r[0].isoformat()
        for r in con.execute(
            f"""SELECT DISTINCT (event_time - INTERVAL {cut} HOUR)::DATE AS d
                FROM read_parquet('{ev_glob}') ORDER BY 1"""
        ).fetchall()
    ]
    print(f"door data covers {len(dates)} service dates: {dates[0]} .. {dates[-1]}")

    for i, d in enumerate(dates, 1):
        dst = out_dir / f"service_date={d}.parquet"
        if dst.exists() and not force:
            continue
        t0 = time.time()
        tmp = dst.with_suffix(".parquet.tmp")
        con.execute(
            f"""
            COPY (
              WITH ev AS (
                SELECT bus_id,
                       (event_time AT TIME ZONE '{city.tz}') AS t_utc,
                       dwell_s, (ron + fon) AS ons, (roff + foff) AS offs,
                       passenger_load
                FROM read_parquet('{ev_glob}')
                WHERE (event_time - INTERVAL {cut} HOUR)::DATE = DATE '{d}'
              ),
              covered AS (SELECT DISTINCT bus_id FROM ev),
              tr AS (
                SELECT trip_key, seg_id, shape_id, vehicle_id,
                       t_enter_utc, t_exit_utc
                FROM read_parquet('{trav_glob}')
                WHERE service_date = DATE '{d}'
                  AND vehicle_id IN (SELECT bus_id FROM covered)
              ),
              agg AS (
                SELECT
                  tr.trip_key, tr.seg_id, tr.shape_id,
                  any_value(tr.vehicle_id) AS vehicle_id,
                  any_value(tr.t_enter_utc) AS t_enter_utc,
                  count(ev.bus_id)::SMALLINT AS door_n,
                  coalesce(sum(ev.dwell_s), 0)::FLOAT AS dwell_s,
                  coalesce(sum(ev.ons), 0)::SMALLINT AS ons,
                  coalesce(sum(ev.offs), 0)::SMALLINT AS offs,
                  coalesce(sum(ev.passenger_load), 0)::INT AS load_sum
                FROM tr
                LEFT JOIN ev
                  ON ev.bus_id = tr.vehicle_id
                 AND ev.t_utc BETWEEN tr.t_enter_utc AND tr.t_exit_utc
                GROUP BY 1, 2, 3
              )
              -- load_in: the load the bus CARRIES INTO the segment = the
              -- passenger_load reported when leaving the most recent stop at
              -- or before segment entry (valid until the next stop starts;
              -- non-dwell delays occur between stops, per user definition).
              SELECT
                agg.trip_key, agg.seg_id, agg.shape_id,
                agg.door_n, agg.dwell_s, agg.ons, agg.offs, agg.load_sum,
                coalesce(ev2.passenger_load, 0)::SMALLINT AS load_in
              FROM agg
              ASOF LEFT JOIN ev ev2
                ON ev2.bus_id = agg.vehicle_id
               AND ev2.t_utc <= agg.t_enter_utc
            ) TO '{tmp}' (FORMAT PARQUET, COMPRESSION ZSTD)
            """
        )
        os.replace(tmp, dst)
        if i % 10 == 0 or i == len(dates):
            print(f"  [{i}/{len(dates)}] {d} ({time.time()-t0:.1f}s/date)", flush=True)

    # summary
    glob = str(out_dir / "service_date=*.parquet")
    r = con.execute(
        f"""SELECT count(*), sum(door_n), round(avg(dwell_s),1),
                   count(DISTINCT trip_key)
            FROM read_parquet('{glob}')"""
    ).fetchone()
    print(f"sidecar: {r[0]:,} covered traversal rows, {r[1]:,} door events joined, "
          f"mean dwell {r[2]}s, {r[3]:,} trips")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--city", default="cta")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--verify-tz", action="store_true")
    args = ap.parse_args()
    city = get_city(args.city)
    if args.verify_tz:
        print(json.dumps(verify_timezone(city), indent=1))
        return
    build_sidecar(city, args.force)


if __name__ == "__main__":
    main()
