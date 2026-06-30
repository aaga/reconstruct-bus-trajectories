"""Build the per-segment free-flow travel-time table.

Scour the entire R2 archive for completed full-length Route 22 SB pattern-3936
trips whose start time (in America/Chicago) falls between 22:00 and 05:00,
reconstruct them all at bandwidth 5, then compute the p5 (95th-percentile-
fastest) of segment travel times across the late-night sample. Cached to
``out_decomposition/freeflow_segments.json``.

Usage:
    PYTHONPATH=src uv run python scripts/decomposition/build_freeflow_baseline.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

from bus_trajectories.delay_decomposition import build_segments_for_pattern  # noqa: E402
from bus_trajectories.delay_decomposition.travel_time import (  # noqa: E402
    save_freeflow_table,
    segment_freeflow_table,
)
from run_r2_route22_sb import R2_PUB, fetch, to_avl_csv_format  # noqa: E402

PATTERN_ID = "3936"
SHAPE_ID = "67803936"
BANDWIDTH = 5
INTERSECTIONS_JSON = REPO / "intersections_route22.json"
GTFS_ZIP = REPO / "cta_gtfs.zip"
R2_CACHE = REPO / "r2_cache"

OUT_DIR = REPO / "out_decomposition"
LATENIGHT_DIR = REPO / f"out_freeflow_latenight_bw{BANDWIDTH}"
LATENIGHT_CSV = REPO / "r2_route22_freeflow_latenight.csv"
FF_TABLE_PATH = OUT_DIR / "freeflow_segments.json"
LATENIGHT_INDEX = OUT_DIR / "latenight_trips_index.csv"

MIN_PINGS = 30
MIN_FULL_LAT_SPAN_DEG = 0.12
TRIP_END_MARGIN_S = 300
MAX_TRIP_DURATION_HRS = 4
CHI_HOUR_START = 22  # 10 PM Chicago
CHI_HOUR_END = 5     # 05:00 Chicago (exclusive upper)


def _fetch_all_cta_hours() -> pd.DataFrame:
    """Download every CTA hour-file in the R2 manifest; return concat'd df."""
    manifest_path = fetch(f"{R2_PUB}/_manifest.parquet",
                         R2_CACHE / "_manifest.parquet")
    manifest = pq.read_table(manifest_path).to_pandas()
    cta = manifest[manifest.agency == "cta"].sort_values(
        ["year", "month", "day", "hour"]
    ).reset_index(drop=True)
    print(f"Scouring R2: {len(cta)} CTA hour-file(s) across "
          f"{cta.groupby(['year','month','day']).ngroups} day(s)")
    parts: list[pd.DataFrame] = []
    for i, row in cta.iterrows():
        if i % 12 == 0:
            print(f"  [{i+1}/{len(cta)}] {row.year:04d}-{row.month:02d}-"
                  f"{row.day:02d} {row.hour:02d}Z")
        local = R2_CACHE / row.path.replace("/", "__")
        fetch(f"{R2_PUB}/{row.path}", local)
        df = pq.ParquetFile(local).read().to_pandas()
        parts.append(df[df.route_id == "22"])
    return pd.concat(parts, ignore_index=True)


def _select_latenight_sb_trips(r22: pd.DataFrame) -> pd.DataFrame:
    """Filter to full-length completed SB pattern-3936 trips whose start time
    in Chicago is within [22:00, 05:00)."""
    r22 = (
        r22.drop_duplicates(["vehicle_id", "timestamp"])
        .sort_values(["vehicle_id", "trip_id", "timestamp"])
        .reset_index(drop=True)
    )
    data_end_ts = r22.timestamp.max()
    end_cutoff = data_end_ts - pd.Timedelta(seconds=TRIP_END_MARGIN_S)

    keep_keys: list[tuple] = []
    for (vid, tid), g in r22.groupby(["vehicle_id", "trip_id"]):
        if len(g) < MIN_PINGS:
            continue
        first_ts = g.timestamp.iloc[0]
        last_ts = g.timestamp.iloc[-1]
        dur_s = (last_ts - first_ts).total_seconds()
        if dur_s <= 0 or dur_s > MAX_TRIP_DURATION_HRS * 3600:
            continue
        if g.latitude.iloc[-1] >= g.latitude.iloc[0]:
            continue  # not SB
        if g.latitude.iloc[0] - g.latitude.iloc[-1] < MIN_FULL_LAT_SPAN_DEG:
            continue
        if last_ts > end_cutoff:
            continue
        chi_hour = first_ts.tz_convert("America/Chicago").hour
        in_window = (chi_hour >= CHI_HOUR_START) or (chi_hour < CHI_HOUR_END)
        if not in_window:
            continue
        keep_keys.append((vid, tid))
    print(f"Late-night SB trips selected: {len(keep_keys)}")
    if not keep_keys:
        raise SystemExit("no late-night SB trips found")
    keep_set = set(keep_keys)
    mask = pd.Series(
        list(zip(r22.vehicle_id, r22.trip_id))
    ).isin(keep_set).values
    return r22[mask].copy()


def _reconstruct_latenight(sb: pd.DataFrame) -> None:
    """Write AVL CSV and run the reconstruct CLI.

    CTA reuses BusTime trip_ids across days/vehicles, so we suffix each
    trip_id with ``_<vehicle_id>_<YYYY-MM-DD>`` before serializing to CSV.
    Otherwise reconstruct_csv (which groups by trip_id alone) would merge
    pings from multiple days into one absurd 10000-minute "trip".
    """
    sb = sb.copy()
    chi_date = sb.timestamp.dt.tz_convert("America/Chicago").dt.date.astype(str)
    sb["trip_id"] = (
        sb.trip_id.astype(str) + "_" + sb.vehicle_id.astype(str) + "_" + chi_date
    )
    LATENIGHT_CSV.parent.mkdir(parents=True, exist_ok=True)
    to_avl_csv_format(sb, LATENIGHT_CSV, pattern_id=PATTERN_ID)
    print(f"Wrote AVL CSV: {LATENIGHT_CSV} ({LATENIGHT_CSV.stat().st_size:,} bytes)")

    if LATENIGHT_DIR.exists():
        shutil.rmtree(LATENIGHT_DIR)

    cmd = [
        sys.executable, "-m", "bus_trajectories", "reconstruct",
        str(LATENIGHT_CSV),
        "--gtfs", str(GTFS_ZIP),
        "--route", "22",
        "--pattern", PATTERN_ID,
        "--bandwidth", str(BANDWIDTH),
        "--serialize",
        "--out", str(LATENIGHT_DIR),
    ]
    print(f"-> {' '.join(cmd)}")
    env = {**__import__("os").environ, "PYTHONPATH": str(REPO / "src")}
    subprocess.check_call(cmd, env=env)


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if (LATENIGHT_DIR / "trajectories.json").exists():
        print(f"Reusing existing late-night reconstructions in {LATENIGHT_DIR}")
    else:
        r22 = _fetch_all_cta_hours()
        print(f"Total Route 22 pings: {len(r22):,}")
        sb = _select_latenight_sb_trips(r22)
        # Persist the trip index for inspection.
        idx = (
            sb.groupby(["vehicle_id", "trip_id"]).timestamp.agg(["min", "max", "count"])
              .reset_index()
              .rename(columns={"min": "first_ts", "max": "last_ts", "count": "n_pings"})
        )
        idx["dur_min"] = (idx.last_ts - idx.first_ts).dt.total_seconds() / 60
        idx["first_ts_chi"] = idx.first_ts.dt.tz_convert("America/Chicago")
        idx.to_csv(LATENIGHT_INDEX, index=False)
        print(f"Wrote late-night index: {LATENIGHT_INDEX}")
        _reconstruct_latenight(sb)

    segments = build_segments_for_pattern(
        PATTERN_ID, INTERSECTIONS_JSON, GTFS_ZIP
    )
    print(f"Built {len(segments)} signal-to-signal segments")
    table = segment_freeflow_table(
        LATENIGHT_DIR / "trajectories.json", segments, percentile=5
    )
    save_freeflow_table(table, FF_TABLE_PATH)
    print(f"Wrote per-segment free-flow table: {FF_TABLE_PATH} "
          f"({len(table)}/{len(segments)} segments covered)")

    # Report quick stats.
    total_ff = sum(table.values()) / 60
    print(f"Sum of segment T_ff = {total_ff:.1f} min (corridor-level p5 estimate)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
