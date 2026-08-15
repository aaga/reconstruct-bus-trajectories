"""Ingest the CTA-highfreq VTRAK hour-parquets into the shared archive
cache as agency=cta-hf hour files (2026-08-05 investigation).

Source: OneDrive highfreq-VTRAK/<date>/<HH>.parquet — 3 vehicles at ~2 s
cadence. Real fix time is ``dtime`` (naive Chicago); the ``timestamp``
column is the poll instant. The feed carries NO trip/route attribution, so
each ping inherits (trip_id, route_id) from the nearest-in-time R2 BusTime
row of the same vehicle (within ±5 min; unattributed pings are dropped).

Output rows carry the columns run_reconstruct._service_date_pings consumes
(timestamp UTC, trip_id, route_id, vehicle_id, latitude, longitude) plus
speed/heading, re-bucketed into UTC hour files.

Usage:
    PYTHONPATH=src uv run python analysis/network/highfreq_ingest.py \
        [--src <dir>] [--force]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from dataio.cities import get_city  # noqa: E402
from dataio.vtrak import chicago_to_utc  # noqa: E402

SRC_DEFAULT = ("/Users/ashwinagarwal/Library/CloudStorage/"
               "OneDrive-ChicagoTransitAuthority/highfreq-VTRAK")
MATCH_TOL_S = 300


def r2_rows_for(cta, vehicles: set[str], hours: list[pd.Timestamp]) -> pd.DataFrame:
    """(vehicle_id, ts_utc, trip_id, route_id) from cached R2 hour files."""
    cache = cta.resolve(cta.archive_cache_dir)
    frames = []
    for h in hours:
        p = cache / (f"agency={cta.r2_agency}__year={h.year:04d}__month="
                     f"{h.month:02d}__day={h.day:02d}__hour={h.hour:02d}.parquet")
        if not p.exists() or p.stat().st_size == 0:
            continue
        df = pq.read_table(
            p, columns=["vehicle_id", "timestamp", "trip_id", "route_id"]
        ).to_pandas()
        frames.append(df[df.vehicle_id.astype(str).isin(vehicles)])
    if not frames:
        return pd.DataFrame(columns=["vehicle_id", "ts_utc", "trip_id", "route_id"])
    out = pd.concat(frames, ignore_index=True)
    out["ts_utc"] = pd.to_datetime(out["timestamp"], utc=True).astype("datetime64[ns, UTC]")
    out["vehicle_id"] = out["vehicle_id"].astype(str)
    return (out[["vehicle_id", "ts_utc", "trip_id", "route_id"]]
            .dropna(subset=["trip_id"])
            .drop_duplicates(["vehicle_id", "ts_utc"])
            .sort_values("ts_utc"))


def ingest(src: str, force: bool) -> None:
    cta = get_city("cta")
    hf = get_city("cta-hf")
    cache = hf.resolve(hf.archive_cache_dir)
    cache.mkdir(parents=True, exist_ok=True)

    for date_dir in sorted(Path(src).iterdir()):
        if not date_dir.is_dir():
            continue
        frames = []
        for hp in sorted(date_dir.glob("*.parquet")):
            df = pq.read_table(hp).to_pandas()
            df = df.rename(columns={"veH_ID": "vehicle_id"})
            df["ts_utc"] = chicago_to_utc(
                pd.to_datetime(df["dtime"], format="%m-%d-%Y %H:%M:%S")
            ).astype("datetime64[ns, UTC]")
            df["vehicle_id"] = df["vehicle_id"].astype(str)
            frames.append(df.dropna(subset=["ts_utc"]))
        if not frames:
            continue
        day = (pd.concat(frames, ignore_index=True)
               .drop_duplicates(["vehicle_id", "ts_utc"])
               .sort_values("ts_utc"))
        vehicles = set(day.vehicle_id.unique())
        hours = sorted(day.ts_utc.dt.floor("h").unique())
        r2 = r2_rows_for(cta, vehicles, [pd.Timestamp(h) for h in hours])
        if r2.empty:
            print(f"{date_dir.name}: no R2 attribution rows — skipped")
            continue
        merged = []
        for veh, g in day.groupby("vehicle_id"):
            ref = r2[r2.vehicle_id == veh]
            if ref.empty:
                continue
            m = pd.merge_asof(
                g.sort_values("ts_utc"), ref[["ts_utc", "trip_id", "route_id"]],
                on="ts_utc", direction="nearest",
                tolerance=pd.Timedelta(seconds=MATCH_TOL_S))
            merged.append(m.dropna(subset=["trip_id"]))
        if not merged:
            print(f"{date_dir.name}: nothing attributed — skipped")
            continue
        out = pd.concat(merged, ignore_index=True)
        out["timestamp"] = out["ts_utc"]
        n_written = 0
        for h, g in out.groupby(out.ts_utc.dt.floor("h")):
            p = cache / (f"agency={hf.r2_agency}__year={h.year:04d}__month="
                         f"{h.month:02d}__day={h.day:02d}__hour={h.hour:02d}.parquet")
            if p.exists() and not force:
                continue
            tbl = pa.Table.from_pandas(
                g[["timestamp", "trip_id", "route_id", "vehicle_id",
                   "latitude", "longitude", "speed", "heading"]],
                preserve_index=False)
            pq.write_table(tbl, p, compression="zstd")
            n_written += 1
        print(f"{date_dir.name}: {len(day):,} pings, {len(out):,} attributed "
              f"({100*len(out)/len(day):.0f}%), {out.trip_id.nunique()} trips, "
              f"{n_written} hour files")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--src", default=SRC_DEFAULT)
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    ingest(args.src, args.force)


if __name__ == "__main__":
    main()
