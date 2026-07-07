"""Scour the R2 archive for ALL completed full-length Route 22 SB pattern-3936
trips (any time of day, weekday or weekend), reconstruct at bw=5, and write
the bundle to ``out_r2_bw5/trajectories.json``.

This is the daytime+full-day pool that downstream decomposition and the
F4/G/H figures consume.

Pipeline per trip:
  1. Group pings by ``(vehicle_id, trip_id, Chicago date)`` so BusTime's
     weekly trip_id reuse doesn't collapse two real trips into one.
  2. Map-match each ping to the route polyline, **truncate at the first
     ping that reaches within ``TERM_TOL_M`` of the shape end**. This
     drops the terminal-layover tail that CTA's AVL keeps reporting
     after a trip is effectively over — otherwise the layover gets
     attributed to the Harrison signal during decomposition.
  3. Apply pass/fail gates on the *truncated* ping series (MIN_PINGS,
     no-inter-ping-gap, max duration, origin proximity, full-lat-span,
     completed-by-cutoff).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
import corridor  # noqa: E402 -- centralized study-corridor constants

from dataio.gtfs import load_gtfs_shape_with_dist  # noqa: E402
from core.mapmatch.shape_snap import SnapToShapeMatcher  # noqa: E402
from dataio.realtime import to_avl_csv_format, load_all_cta_hours  # noqa: E402

PATTERN_ID = corridor.PATTERN_ID
SHAPE_ID = corridor.SHAPE_ID
BANDWIDTH = 5
ARCHIVE_CACHE = REPO / "caches" / "realtime_archive"
GTFS_ZIP = REPO / "data" / "gtfs" / "cta_gtfs.zip"

OUT_DIR = REPO / "outputs" / "out_r2_bw5"
ALL_CSV = REPO / "data" / "r2_route22_sb_all.csv"
OUT_DECOMP = REPO / "outputs" / "out_decomposition"
ALL_INDEX = OUT_DECOMP / "all_sb_trips_index.csv"

MIN_PINGS = 30
MIN_FULL_LAT_SPAN_DEG = 0.12
TRIP_END_MARGIN_S = 300
MAX_TRIP_DURATION_HRS = 4

# Terminal-truncation constants. ``TERM_TOL_M`` is set looser than the F4
# script's 50 m because the GTFS shape extends ~30-90 m past the actual
# Harrison terminal stop where most SB buses park; tightening below ~100 m
# would drop the majority of clean trips (e.g. the canonical
# ``1001350_4017_2026-05-05`` stops 89 m short of the shape end). 150 m
# keeps ~94% of candidates and still rejects the suspicious tail
# (gap > 170 m is the 95th percentile of all candidate trips).
TERM_TOL_M = 150.0
ORIGIN_TOL_M = 600.0     # within 600 m of shape start at first ping
GAP_MAX_S = 300          # 5-min max inter-ping gap on truncated series


def _fetch_all_cta_hours() -> pd.DataFrame:
    """All Route 22 pings across the entire R2 archive (cached locally)."""
    return load_all_cta_hours(cache_dir=ARCHIVE_CACHE, route_id=corridor.ROUTE_ID)


def _select_all_sb_trips(r22: pd.DataFrame) -> pd.DataFrame:
    """Filter to full-length completed SB pattern-3936 trips (any hour) and
    truncate each at first terminal arrival.

    CTA reuses BusTime trip_ids ~weekly, so the same ``(vehicle_id, trip_id)``
    pair recurs across multiple Chicago dates. We disambiguate *before*
    grouping by attaching the Chicago date to the grouping key — otherwise
    a single recurring pair (e.g. 4017/1001350 on 5/5 and 5/15) is treated
    as one >4-hour "trip" and rejected by ``MAX_TRIP_DURATION_HRS``.

    Terminal truncation (lifted from ``build_alltrip_aligned.select_and_truncate``)
    map-matches every ping to the shape polyline and discards pings after the
    first one that reaches within ``TERM_TOL_M`` of the shape end. This
    eliminates terminal-layover tails (motionless reporting after the trip
    is effectively over) that otherwise inflate signal_uniform attribution
    at the Harrison signal.
    """
    r22 = r22.drop_duplicates(["vehicle_id", "timestamp"]).copy()
    r22["chi_date"] = (
        r22.timestamp.dt.tz_convert("America/Chicago").dt.date.astype(str)
    )
    r22 = (
        r22.sort_values(["vehicle_id", "trip_id", "chi_date", "timestamp"])
        .reset_index(drop=True)
    )
    data_end_ts = r22.timestamp.max()
    end_cutoff = data_end_ts - pd.Timedelta(seconds=TRIP_END_MARGIN_S)

    # Build the shape matcher once for the entire selection loop.
    poly, dist_along = load_gtfs_shape_with_dist(GTFS_ZIP, SHAPE_ID)
    shape_len_m = float(dist_along[-1])
    matcher = SnapToShapeMatcher(
        polyline_latlon=poly,
        dist_along_m_per_vertex=dist_along,
        max_perp_m=80.0,
    )
    print(f"Shape {SHAPE_ID} length = {shape_len_m:.1f} m "
          f"({shape_len_m / 1609.344:.2f} mi); "
          f"terminal-tol = {TERM_TOL_M:.0f} m")

    stats = {
        "too_few_pings": 0, "not_sb": 0, "too_long": 0, "after_cutoff": 0,
        "not_starting_at_origin": 0, "no_terminal_reach": 0,
        "gap_too_long": 0, "lat_span_too_small": 0, "kept": 0,
    }
    kept_chunks: list[pd.DataFrame] = []
    for (vid, tid, chi_date), g in r22.groupby(
        ["vehicle_id", "trip_id", "chi_date"]
    ):
        if len(g) < MIN_PINGS:
            stats["too_few_pings"] += 1
            continue

        # Map-match the FULL trip first so terminal-arrival can be detected.
        m = matcher.match(g.latitude.to_numpy(), g.longitude.to_numpy())
        d = m.dist_along_m
        on = m.on_route

        # Origin check: first ping must snap close to start of shape.
        if d[0] > ORIGIN_TOL_M:
            stats["not_starting_at_origin"] += 1
            continue

        # Find first index that has reached the terminal.
        at_term = (d >= shape_len_m - TERM_TOL_M) & on
        if not at_term.any():
            stats["no_terminal_reach"] += 1
            continue
        end_idx = int(np.argmax(at_term))  # first True
        trunc = g.iloc[: end_idx + 1].copy()
        if len(trunc) < MIN_PINGS:
            stats["too_few_pings"] += 1
            continue

        # Gap check (on truncated series only — terminal-layover gaps no
        # longer count against the trip).
        gaps_s = trunc.timestamp.diff().dt.total_seconds().fillna(0).to_numpy()
        if (gaps_s > GAP_MAX_S).any():
            stats["gap_too_long"] += 1
            continue

        # Post-truncation duration / direction / completeness gates.
        first_ts = trunc.timestamp.iloc[0]
        last_ts = trunc.timestamp.iloc[-1]
        dur_s = (last_ts - first_ts).total_seconds()
        if dur_s <= 0 or dur_s > MAX_TRIP_DURATION_HRS * 3600:
            stats["too_long"] += 1
            continue
        if trunc.latitude.iloc[-1] >= trunc.latitude.iloc[0]:
            stats["not_sb"] += 1
            continue
        if trunc.latitude.iloc[0] - trunc.latitude.iloc[-1] < MIN_FULL_LAT_SPAN_DEG:
            stats["lat_span_too_small"] += 1
            continue
        if last_ts > end_cutoff:
            stats["after_cutoff"] += 1
            continue

        kept_chunks.append(trunc)
        stats["kept"] += 1

    print("Filter outcomes:")
    for k, v in stats.items():
        print(f"  {k:>26}: {v}")
    if not kept_chunks:
        raise SystemExit("no SB trips found")
    out = pd.concat(kept_chunks, ignore_index=True)
    n_unique = out.groupby(["vehicle_id", "trip_id", "chi_date"]).ngroups
    print(f"\nKept {n_unique} SB trips, {len(out):,} truncated pings.")
    return out


def _reconstruct(sb: pd.DataFrame) -> None:
    """Disambiguate trip_ids by appending _<vehicle_id>_<date>, write the AVL
    CSV, and call the reconstruct CLI."""
    sb = sb.copy()
    if "chi_date" not in sb.columns:
        sb["chi_date"] = (
            sb.timestamp.dt.tz_convert("America/Chicago").dt.date.astype(str)
        )
    sb["trip_id"] = (
        sb.trip_id.astype(str) + "_" + sb.vehicle_id.astype(str) + "_" + sb.chi_date
    )
    ALL_CSV.parent.mkdir(parents=True, exist_ok=True)
    to_avl_csv_format(sb, ALL_CSV, pattern_id=PATTERN_ID)
    print(f"Wrote AVL CSV: {ALL_CSV} ({ALL_CSV.stat().st_size:,} bytes)")

    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)

    cmd = [
        sys.executable, "-m", "cli", "reconstruct",
        str(ALL_CSV),
        "--gtfs", str(REPO / "data" / "gtfs" / "cta_gtfs.zip"),
        "--route", corridor.ROUTE_ID,
        "--pattern", PATTERN_ID,
        "--bandwidth", str(BANDWIDTH),
        "--serialize",
        "--out", str(OUT_DIR),
    ]
    print(f"-> {' '.join(cmd)}")
    env = {**__import__("os").environ, "PYTHONPATH": str(REPO / "src")}
    subprocess.check_call(cmd, env=env)


def main() -> int:
    OUT_DECOMP.mkdir(parents=True, exist_ok=True)
    r22 = _fetch_all_cta_hours()
    print(f"Total Route 22 pings: {len(r22):,}")
    sb = _select_all_sb_trips(r22)
    idx = (
        sb.groupby(["vehicle_id", "trip_id", "chi_date"])
          .timestamp.agg(["min", "max", "count"])
          .reset_index()
          .rename(columns={"min": "first_ts", "max": "last_ts", "count": "n_pings"})
    )
    idx["dur_min"] = (idx.last_ts - idx.first_ts).dt.total_seconds() / 60
    idx["first_ts_chi"] = idx.first_ts.dt.tz_convert("America/Chicago")
    idx.to_csv(ALL_INDEX, index=False)
    print(f"Wrote all-SB index: {ALL_INDEX}")
    _reconstruct(sb)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
