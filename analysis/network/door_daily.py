"""One-time split of the monthly bus-state export into per-service-date files.

delay_events._door_intervals re-scanned up to three ~230 MB month files for
EVERY service date (the 03:00 cutover straddles months), which at 957 dates
is ~1-1.5 h of pure re-reading. This writes
outputs/network/<city>/door_daily/service_date=YYYY-MM-DD.parquet once;
the reader then touches only its own date's few-MB file.

Columns are exactly what _door_intervals needs, pre-converted: t_open
(epoch seconds), dwell_s, passenger_load, latitude, longitude, stop_id
(null in the historical export), trip_id + trip_start (for the layover
sanitizer's trip grouping).

Usage:
    PYTHONPATH=src uv run python analysis/network/door_daily.py --city cta
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


def build(city_id: str, force: bool = False) -> None:
    city = get_city(city_id)
    if not city.door_source_dir:
        raise SystemExit(f"{city_id} has no door_source_dir")
    src = Path(city.door_source_dir)
    out = REPO / "outputs" / "network" / city.city_id / "door_daily"
    months = sorted(src.glob("month=*.parquet"))
    if not months:
        raise SystemExit(f"no month files under {src}")
    con = duckdb.connect()
    con.execute("SET preserve_insertion_order=false")
    t0 = time.time()
    import pyarrow.parquet as pq
    cols = set(pq.ParquetFile(months[0]).schema.names)
    dwell = "dwell_s" if "dwell_s" in cols else "dwell_time"
    stop = "stop_id" if "stop_id" in cols else "NULL"
    done = 0
    for mf in months:
        month = mf.stem.split("=")[1]
        # a month's cutover rows land on its first/last dates; presence of
        # the month's mid-date file marks the month done
        probe = out / f"service_date={month[:4]}-{month[4:]}-15.parquet"
        if probe.exists() and not force:
            done += 1
            continue
        con.execute(f"""
            COPY (
              SELECT CAST((event_time - INTERVAL {city.service_day_cutover_h}
                           HOUR) AS DATE) AS service_date,
                     CAST(bus_id AS VARCHAR) AS bus_id,
                     epoch(event_time AT TIME ZONE '{city.tz}') AS t_open,
                     coalesce({dwell}, 0) AS dwell_s,
                     passenger_load,
                     latitude, longitude,
                     {stop} AS stop_id,
                     CAST(trip_id AS VARCHAR) AS d_trip,
                     CAST(trip_start_time AS VARCHAR) AS d_trip_start,
                     ron, roff, fon, foff
              FROM read_parquet('{mf}')
              WHERE coalesce(ron,0)+coalesce(roff,0)
                    +coalesce(fon,0)+coalesce(foff,0) > 0
              ORDER BY service_date, bus_id, t_open
            ) TO '{out}' (FORMAT PARQUET, COMPRESSION ZSTD,
                          PARTITION_BY (service_date),
                          OVERWRITE_OR_IGNORE, FILENAME_PATTERN 'part{{i}}')
        """)
        print(f"  {month} split ({time.time() - t0:.0f}s)", flush=True)
    n = len(list(out.glob("service_date=*")))
    print(f"door_daily: {n} service dates ({done} months already present)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--city", default="cta")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    build(a.city, a.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
