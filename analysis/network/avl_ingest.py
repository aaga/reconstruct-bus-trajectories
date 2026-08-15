"""Ingest a local high-resolution AVL export into archive hour-files.

Converts ``city.avl_source_dir`` daily parquet (redshift export: naive
LOCAL ``avl_event_time``, speeds in FEET PER SECOND — verified empirically
2026-08-15: median implied/reported ratio 0.308≈0.3048 across 3 days,
n>230k steady straight-heading intervals; 255 is the u8 sentinel) into the
same hour-file layout the R2 prefetch produces, under ``city.r2_agency``
(cta → ``agency=cta-rs``). The batch readers then work unchanged, with one
addition: a ``speed_mps`` column that routes reconstruction to VCHIP-ME.

Only in-service rows are kept (onroute=1, route & trip present) — matching
the R2 scrape's semantics.

Usage:
    PYTHONPATH=src uv run python analysis/network/avl_ingest.py --city cta
    ... --force        # rewrite existing hour files
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

FPS_TO_MPS = 0.3048
SPEED_SENTINEL = 255.0


def ingest(city_id: str, force: bool = False) -> None:
    city = get_city(city_id)
    if not city.avl_source_dir:
        raise SystemExit(f"{city_id} has no avl_source_dir configured")
    src = Path(city.avl_source_dir)
    cache = city.resolve(city.archive_cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    days = sorted(src.glob("date=*.parquet"))
    if not days:
        raise SystemExit(f"no date=*.parquet under {src}")
    con = duckdb.connect()
    n_files = n_rows = n_skipped = 0
    t0 = time.time()
    for day in days:
        hours = con.execute(f"""
            SELECT DISTINCT date_trunc('hour',
                     avl_event_time AT TIME ZONE '{city.tz}') AS h
            FROM '{day}' ORDER BY h
        """).fetchall()
        for (h,) in hours:
            out = cache / (
                f"agency={city.r2_agency}__year={h.year:04d}__month="
                f"{h.month:02d}__day={h.day:02d}__hour={h.hour:02d}.parquet")
            if out.exists() and out.stat().st_size > 0 and not force:
                n_skipped += 1
                continue
            con.execute(f"""
                COPY (
                  SELECT
                    avl_event_time AT TIME ZONE '{city.tz}' AS timestamp,
                    CAST(trip_id AS VARCHAR)  AS trip_id,
                    CAST(route_id AS VARCHAR) AS route_id,
                    CAST(bus_id AS VARCHAR)   AS vehicle_id,
                    latitude, longitude,
                    CASE WHEN speed IS NULL OR speed >= {SPEED_SENTINEL}
                         THEN NULL
                         ELSE speed * {FPS_TO_MPS} END AS speed_mps
                  FROM '{day}'
                  WHERE onroute = 1 AND route_id IS NOT NULL
                    AND trip_id IS NOT NULL
                    AND latitude IS NOT NULL AND longitude IS NOT NULL
                    AND date_trunc('hour',
                          avl_event_time AT TIME ZONE '{city.tz}')
                        = TIMESTAMPTZ '{h}'
                  ORDER BY timestamp
                ) TO '{out}' (FORMAT PARQUET)
            """)
            n_files += 1
            n_rows += con.execute(
                f"SELECT count(*) FROM '{out}'").fetchone()[0]
        print(f"  {day.name}: done ({time.time() - t0:.0f}s)", flush=True)
    print(f"wrote {n_files} hour files ({n_rows:,} rows), "
          f"skipped {n_skipped} existing → {cache}/agency={city.r2_agency}__*")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--city", default="cta")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    ingest(args.city, force=args.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
