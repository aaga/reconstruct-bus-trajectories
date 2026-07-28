"""Ingest CTA bus-state-history door-event CSVs into local parquet.

One row per completed door cycle: bus_id, event_time, event_type, trip_id,
dwell_time (door-open seconds), APC counts (ron/roff = rear ons/offs,
fon/foff = front), passenger_load. Source CSVs live on slow cloud storage —
this converts them ONCE into ``caches/door_events/`` (gitignored) partitioned
by service month; everything downstream reads the parquet.

Timestamps are stored as naive wall-clock exactly as in the CSV; the timezone
is established empirically by ``infer.py``-style checks in door_join (CTA
convention is America/Chicago — verified against AVL trajectories).

Usage:
    PYTHONPATH=src uv run python analysis/network/door_events.py \
        --city cta "<csv1>" "<csv2>" ...
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import duckdb

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from dataio.cities import get_city  # noqa: E402


def ingest(city_id: str, csvs: list[str]) -> Path:
    city = get_city(city_id)
    out_dir = city.resolve("caches/door_events") / city.city_id
    out_dir.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect()
    for csv in csvs:
        src = Path(csv)
        dst = out_dir / (src.stem + ".parquet")
        if dst.exists() and dst.stat().st_size > 0:
            print(f"cached: {dst.name}")
            continue
        t0 = time.time()
        print(f"converting {src.name} …", flush=True)
        con.execute(
            f"""
            COPY (
              SELECT
                bus_id::VARCHAR            AS bus_id,
                event_time::TIMESTAMP      AS event_time,
                event_type::TINYINT        AS event_type,
                trip_id::VARCHAR           AS trip_id,
                trip_start_time::TIMESTAMP AS trip_start_time,
                dwell_time::FLOAT          AS dwell_s,
                ron::SMALLINT AS ron, roff::SMALLINT AS roff,
                fon::SMALLINT AS fon, foff::SMALLINT AS foff,
                passenger_load::SMALLINT   AS passenger_load
              FROM read_csv('{src}', header=true)
            ) TO '{dst}' (FORMAT PARQUET, COMPRESSION ZSTD)
            """
        )
        n = con.execute(f"SELECT count(*) FROM read_parquet('{dst}')").fetchone()[0]
        print(f"  {n:,} rows in {time.time()-t0:.0f}s → {dst.name}", flush=True)

    # Summary across everything ingested.
    glob = str(out_dir / "*.parquet")
    print("\n=== ingest summary ===")
    for row in con.execute(
        f"""
        SELECT strftime(event_time, '%Y-%m') AS month,
               count(*) AS events,
               count(DISTINCT bus_id) AS buses,
               min(event_time) AS t0, max(event_time) AS t1
        FROM read_parquet('{glob}') GROUP BY 1 ORDER BY 1
        """
    ).fetchall():
        print(f"  {row[0]}: {row[1]:,} events, {row[2]} buses, {row[3]} → {row[4]}")
    print("\nevent_type distribution:")
    for row in con.execute(
        f"SELECT event_type, count(*), round(avg(dwell_s),1) FROM read_parquet('{glob}') GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall():
        print(f"  type {row[0]}: {row[1]:,} rows, mean dwell {row[2]}s")
    return out_dir


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--city", default="cta")
    ap.add_argument("csvs", nargs="+")
    args = ap.parse_args()
    ingest(args.city, args.csvs)


if __name__ == "__main__":
    main()
