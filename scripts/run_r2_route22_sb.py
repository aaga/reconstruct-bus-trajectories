"""End-to-end: pull latest R2 CTA pings, filter to Route 22 SB, reconstruct, visualize.

Pipeline:
  1. Read the R2 manifest, find recent CTA hour-files.
  2. Download those parquets, concatenate.
  3. Filter to route_id='22'.
  4. Classify each trip's direction by latitude trend (SB = decreasing).
  5. Pick the latest N completed-looking SB trips.
  6. Write a CSV in the format our existing CLI expects.
  7. Run `reconstruct` at multiple bandwidths.
  8. Run `compare` to produce the interactive HTML viewer.

CTA's BusTime feed gives us route_id, vehicle_id, lat/lon, timestamp, and a
BusTime trip_id (not GTFS-style). It does NOT supply direction_id or
pattern_id, so we recover direction from the trajectory itself and assume
trips going south on Clark are on the main SB pattern (3936). This isn't
strictly guaranteed (some SB trips may be on minor variants 3935 or 5422),
but for sanity-checking the pipeline against shape 67803936 it's fine.
"""

from __future__ import annotations

import argparse
import io as _io
import shutil
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

R2_PUB = "https://pub-777d0904efb449dc838791645b9e2e0f.r2.dev"

# Quality thresholds for picking COMPLETE FULL-LENGTH trips:
#   - MIN_PINGS: the bus reported enough times to give the smoother something
#     to work with (~30 pings ≈ 15-30 minutes of service depending on cadence).
#   - MIN_FULL_LAT_SPAN_DEG: Route 22 SB spans about 0.145° of latitude
#     (Howard 42.02 → Harrison 41.87). Require at least 0.12° = ~80% of the
#     full route to exclude mid-route block continuations and short-turns.
#   - TRIP_END_MARGIN_S: a trip must end at least this long before our data
#     window ends, so we know it actually completed (vs. still in progress).
MIN_PINGS = 30
MIN_FULL_LAT_SPAN_DEG = 0.12
TRIP_END_MARGIN_S = 300


def fetch(url: str, dst: Path) -> Path:
    """Download a URL to dst (uses curl for robustness with R2 public URLs)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and dst.stat().st_size > 0:
        return dst
    print(f"  ↓ {url}")
    subprocess.check_call(["curl", "-sSL", "-o", str(dst), url])
    return dst


def load_recent_cta_hours(n_hours: int, cache_dir: Path) -> pd.DataFrame:
    """Return concatenated CTA pings from the n most recent hour-files."""
    manifest_path = fetch(f"{R2_PUB}/_manifest.parquet", cache_dir / "_manifest.parquet")
    manifest = pq.read_table(manifest_path).to_pandas()
    cta = (
        manifest[manifest.agency == "cta"]
        .sort_values(["year", "month", "day", "hour"], ascending=False)
        .head(n_hours)
    )
    if cta.empty:
        raise SystemExit("no CTA hours in manifest")
    print(f"Pulling {len(cta)} CTA hour(s):")
    parts = []
    for _, row in cta.iterrows():
        url = f"{R2_PUB}/{row.path}"
        # Flatten the hive-style path into a single filename so pyarrow doesn't
        # auto-treat the cache dir as a partitioned dataset.
        local = cache_dir / row.path.replace("/", "__")
        fetch(url, local)
        parts.append(pq.ParquetFile(local).read().to_pandas())
    return pd.concat(parts, ignore_index=True)


def select_route22_sb_trips(df: pd.DataFrame, n: int = 10) -> pd.DataFrame:
    """Pick the n most recently *completed* full-length Route 22 SB trips.

    A "completed full-length" trip is:
      - lat_first > lat_last (southbound),
      - lat_span >= MIN_FULL_LAT_SPAN_DEG (the trip covers most of the route),
      - last_ts <= data_end_ts - TRIP_END_MARGIN_S (we observed the trip end).

    Trips matching all three are sorted by last_ts descending and the top n
    are returned.

    Also dedupes pings on (vehicle_id, timestamp) — CTA's BusTime echoes
    stale positions when a vehicle stops reporting, producing duplicate rows
    that pollute the smoother's input.
    """
    r22 = df[df.route_id == "22"].copy()
    r22 = (
        r22.drop_duplicates(subset=["vehicle_id", "timestamp"])
           .sort_values(["trip_id", "timestamp"])
           .reset_index(drop=True)
    )
    print(f"Route 22 unique pings: {len(r22)}")

    data_end_ts = r22.timestamp.max()
    print(f"Data window end: {data_end_ts} UTC "
          f"(trips must end before {data_end_ts - pd.Timedelta(seconds=TRIP_END_MARGIN_S)})")

    summary_rows = []
    for tid, g in r22.groupby("trip_id"):
        if len(g) < MIN_PINGS:
            continue
        lat_first = g.latitude.iloc[0]
        lat_last = g.latitude.iloc[-1]
        lat_span = abs(lat_first - lat_last)
        if lat_last >= lat_first:
            continue  # not SB
        if lat_span < MIN_FULL_LAT_SPAN_DEG:
            continue  # not full-length
        if g.timestamp.iloc[-1] > data_end_ts - pd.Timedelta(seconds=TRIP_END_MARGIN_S):
            continue  # still in progress
        summary_rows.append({
            "trip_id": tid,
            "bus_id": g.vehicle_id.iloc[0],
            "n_pings": len(g),
            "first_ts": g.timestamp.iloc[0],
            "last_ts": g.timestamp.iloc[-1],
            "duration_min": (g.timestamp.iloc[-1] - g.timestamp.iloc[0]).total_seconds() / 60,
            "lat_span": lat_span,
        })

    summary = pd.DataFrame(summary_rows).sort_values("last_ts", ascending=False)
    if summary.empty:
        raise SystemExit(
            "no completed full-length SB trips in window; try --n-hours larger"
        )
    print(f"Found {len(summary)} completed full-length SB trips. Most recent {n}:")
    print(summary.head(n).to_string(index=False))
    keep = summary.head(n).trip_id.tolist()
    return r22[r22.trip_id.isin(keep)].copy()


def to_avl_csv_format(df: pd.DataFrame, out_csv: Path, pattern_id: str = "3936") -> Path:
    """Write a CSV in the format expected by `bus_trajectories reconstruct`.

    `load_avl_csv` only reads: trip_id, bus_id, route_id, pattern_id,
    avl_event_time, latitude, longitude. The rest of the canonical AVL CSV
    columns are filled with empty strings to match the parser's expectations.
    """
    df = df.copy()
    df["avl_event_time"] = df.timestamp.dt.tz_convert(None).dt.strftime("%Y-%m-%d %H:%M:%S.%f")
    df["bus_id"] = df.vehicle_id
    df["pattern_id"] = pattern_id

    out = pd.DataFrame({
        "id": df.entity_id.fillna("") + "_" + df.timestamp.astype(str),
        "bus_id": df.bus_id,
        "avl_event_time": df.avl_event_time,
        "bt_ver": "",
        "route_id": df.route_id,
        "pattern_id": df.pattern_id,
        "direction": "",
        "deviation": "",
        "speed": "",
        "operator_id": "",
        "last_ob_update": "",
        "garage": "",
        "run_id": "",
        "trip_id": df.trip_id,
        "last_trip_update": "",
        "last_tp_passed": "",
        "last_tp_update": "",
        "latitude": df.latitude,
        "longitude": df.longitude,
        "heading": df.bearing.fillna("").astype(str),
        "onroute": "",
        "mmode": "",
        "last_mmode": "",
        "cta_inserted_dtm_usa_chi": "",
        "service_yearmo": "",
    })
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(out_csv, index=False)
    return out_csv


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-hours", type=int, default=4,
                    help="How many recent CTA hour-files to pull")
    ap.add_argument("--n-trips", type=int, default=10)
    ap.add_argument("--also-include", default="",
                    help="Comma-separated trip_ids to force-include (bypasses "
                         "quality filters; useful for inspecting partial trips)")
    ap.add_argument("--gtfs", default="cta_gtfs.zip")
    ap.add_argument("--pattern", default="3936")
    ap.add_argument("--bandwidths", default="5,7,10,15,20")
    ap.add_argument("--cache", default="r2_cache")
    ap.add_argument("--csv-out", default="r2_route22_sb.csv")
    ap.add_argument("--out-html", default="out_r2_compare.html")
    ap.add_argument("--endpoint", default="http://localhost:8002",
                    help="(unused here; kept for parity)")
    args = ap.parse_args()

    cache_dir = Path(args.cache)
    df = load_recent_cta_hours(args.n_hours, cache_dir)
    sb = select_route22_sb_trips(df, n=args.n_trips)

    # Force-include any extra trip_ids the user asked for (deduped + Route 22 only).
    extras = [t.strip() for t in args.also_include.split(",") if t.strip()]
    if extras:
        extra_df = (
            df[df.route_id == "22"]
              .drop_duplicates(["vehicle_id", "timestamp"])
              .query("trip_id in @extras")
              .sort_values(["trip_id", "timestamp"])
        )
        n_added = extra_df.trip_id.nunique()
        print(f"\nForce-including {n_added} extra trip(s): {sorted(extra_df.trip_id.unique())}")
        sb = pd.concat([sb, extra_df], ignore_index=True).drop_duplicates(
            ["vehicle_id", "timestamp"]
        )

    csv = to_avl_csv_format(sb, Path(args.csv_out), pattern_id=args.pattern)
    print(f"\nWrote: {csv}  ({csv.stat().st_size:,} bytes, {len(sb):,} rows)")

    # Run reconstruct at each bandwidth.
    bws = [int(b) for b in args.bandwidths.split(",")]
    bw_dirs: list[Path] = []
    for bw in bws:
        out_dir = Path(f"out_r2_bw{bw}")
        if out_dir.exists():
            shutil.rmtree(out_dir)
        cmd = [
            sys.executable, "-m", "bus_trajectories", "reconstruct",
            str(csv), "--gtfs", args.gtfs,
            "--route", "22", "--pattern", args.pattern,
            "--bandwidth", str(bw),
            "--serialize",
            "--out", str(out_dir),
        ]
        print(f"\n→ {' '.join(cmd)}")
        env = {**__import__("os").environ, "PYTHONPATH": "src"}
        subprocess.check_call(cmd, env=env)
        bw_dirs.append(out_dir)

    # Build comparison HTML.
    raw_dir = bw_dirs[0]
    cmd = [
        sys.executable, "-m", "bus_trajectories", "compare",
        *[str(d) for d in bw_dirs],
        "--raw-dir", str(raw_dir),
        "--gtfs", args.gtfs, "--pattern", args.pattern,
        "--out", args.out_html,
        "--title", f"R2 latest {args.n_trips} Route 22 SB trips",
    ]
    print(f"\n→ {' '.join(cmd)}")
    env = {**__import__("os").environ, "PYTHONPATH": "src"}
    subprocess.check_call(cmd, env=env)
    print(f"\nDone. Open: {args.out_html}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
