"""Prefetch R2 archive hour-files into the local cache, concurrently.

The batch reconstructor's workers read hour parquet from the local cache only;
run this first so a flaky network never stalls a compute worker. Idempotent —
already-cached files are skipped by ``realtime.fetch``.

Usage:
    PYTHONPATH=src uv run python analysis/network/prefetch.py --city cta
    PYTHONPATH=src uv run python analysis/network/prefetch.py --city cta \
        --start 2026-05-01 --end 2026-05-02   # UTC day bounds, inclusive
"""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from dataio import realtime  # noqa: E402
from dataio.cities import get_city  # noqa: E402


def prefetch(
    city_id: str = "cta",
    start: str | None = None,
    end: str | None = None,
    threads: int = 16,
) -> tuple[int, int]:
    """Fetch all (or a UTC date range of) hour-files. Returns (n_ok, n_fail)."""
    city = get_city(city_id)
    cache_dir = city.resolve(city.archive_cache_dir)
    man = realtime.load_manifest(cache_dir, refresh=True)
    rows = man[man.agency == city.r2_agency].copy()
    rows["dt"] = pd.to_datetime(
        dict(year=rows.year, month=rows.month, day=rows.day, hour=rows.hour), utc=True
    )
    if start:
        rows = rows[rows.dt >= pd.Timestamp(start, tz="UTC")]
    if end:
        rows = rows[rows.dt < pd.Timestamp(end, tz="UTC") + pd.Timedelta(days=1)]

    todo = [
        p
        for p in rows.path
        if not (cache_dir / p.replace("/", "__")).exists()
        or (cache_dir / p.replace("/", "__")).stat().st_size == 0
    ]
    print(f"{len(rows)} hour-files in range; {len(todo)} to fetch")

    n_ok = len(rows) - len(todo)
    n_fail = 0
    with ThreadPoolExecutor(max_workers=threads) as ex:
        futs = {
            ex.submit(
                realtime.fetch,
                f"{realtime.ARCHIVE_URL}/{p}",
                cache_dir / p.replace("/", "__"),
            ): p
            for p in todo
        }
        for i, fut in enumerate(as_completed(futs), 1):
            try:
                fut.result()
                n_ok += 1
            except Exception as e:  # noqa: BLE001 — count, report, continue
                n_fail += 1
                print(f"  FAIL {futs[fut]}: {e}", file=sys.stderr)
            if i % 100 == 0:
                print(f"  {i}/{len(todo)}")
    print(f"done: {n_ok} cached, {n_fail} failed")
    return n_ok, n_fail


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--city", default="cta")
    ap.add_argument("--start", default=None, help="UTC date, inclusive (YYYY-MM-DD)")
    ap.add_argument("--end", default=None, help="UTC date, inclusive (YYYY-MM-DD)")
    ap.add_argument("--threads", type=int, default=16)
    args = ap.parse_args()
    _, n_fail = prefetch(args.city, args.start, args.end, args.threads)
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
