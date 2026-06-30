"""Pull 50 consecutive complete Route 22 SB trips on pattern 3936 starting at
7am Chicago, smooth them, and produce two TS diagrams:

  slides/C1_50trips_clock.png    — actual clock time (spaced out)
  slides/C2_50trips_aligned.png  — minutes since departure (aligned)

"Departure" = last stationary ping before the bus first makes >=0.03 mi of
forward progress along the shape (matches the existing
timespace_route22_aligned_departure.png reference).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pyarrow.parquet as pq

PYTHONPATH_SRC = str((Path(__file__).resolve().parents[2] / "src"))
if PYTHONPATH_SRC not in sys.path:
    sys.path.insert(0, PYTHONPATH_SRC)

from bus_trajectories.pipeline import reconstruct_csv  # noqa: E402
from bus_trajectories.realtime import ARCHIVE_URL, fetch  # noqa: E402

GTFS = "data/gtfs/cta_gtfs.zip"
PATTERN = "3936"
ROUTE = "22"
SHAPE_ID = "67803936"
M_PER_MI = 1609.344
BW = 5
N_TRIPS = 50
START_UTC = pd.Timestamp("2026-05-05 12:00", tz="UTC")  # 7am CDT
CACHE = Path("caches/realtime_archive")
CSV_OUT = Path("data/r2_route22_sb_50.csv")
SLIDES = Path("figures")




def load_pings_from(start_utc: pd.Timestamp) -> pd.DataFrame:
    fetch(f"{ARCHIVE_URL}/_manifest.parquet", CACHE / "_manifest.parquet")
    m = pq.read_table(CACHE / "_manifest.parquet").to_pandas()
    cta = m[m.agency == "cta"].copy()
    cta["dt"] = pd.to_datetime(
        dict(year=cta.year, month=cta.month, day=cta.day, hour=cta.hour),
        utc=True,
    )
    cta = cta[cta.dt >= start_utc].sort_values("dt")
    print(f"Pulling {len(cta)} CTA hour-files from {start_utc} UTC onward…")
    parts = []
    for _, row in cta.iterrows():
        local = CACHE / row.path.replace("/", "__")
        fetch(f"{ARCHIVE_URL}/{row.path}", local)
        parts.append(pq.ParquetFile(local).read().to_pandas())
    return pd.concat(parts, ignore_index=True)


def select_first_n_sb_trips(df: pd.DataFrame, start_utc: pd.Timestamp, n: int) -> pd.DataFrame:
    r22 = df[df.route_id == "22"].copy()
    r22 = r22.drop_duplicates(["vehicle_id", "timestamp"]).sort_values(
        ["trip_id", "timestamp"]
    ).reset_index(drop=True)

    MIN_PINGS = 30
    MIN_LAT_SPAN = 0.12
    END_MARGIN_S = 300
    MAX_DURATION_MIN = 180  # exclude stale-ping garbage (real SB run ≤ ~90 min)
    data_end = r22.timestamp.max()

    rows = []
    for tid, g in r22.groupby("trip_id"):
        if len(g) < MIN_PINGS:
            continue
        lf, ll = g.latitude.iloc[0], g.latitude.iloc[-1]
        if ll >= lf:
            continue
        if abs(lf - ll) < MIN_LAT_SPAN:
            continue
        if g.timestamp.iloc[-1] > data_end - pd.Timedelta(seconds=END_MARGIN_S):
            continue
        dur_min = (g.timestamp.iloc[-1] - g.timestamp.iloc[0]).total_seconds() / 60.0
        if dur_min > MAX_DURATION_MIN:
            continue
        rows.append({
            "trip_id": tid,
            "first_ts": g.timestamp.iloc[0],
            "last_ts": g.timestamp.iloc[-1],
            "n": len(g),
        })
    summary = pd.DataFrame(rows).sort_values("first_ts")
    summary = summary[summary.first_ts >= start_utc].reset_index(drop=True)
    print(f"Completed full-length SB trips at/after cutoff: {len(summary)}")
    keep = summary.head(n).trip_id.tolist()
    if len(keep) < n:
        print(f"WARNING: only {len(keep)} trips available; requested {n}")
    return r22[r22.trip_id.isin(keep)].copy()


def to_avl_csv(df: pd.DataFrame, out: Path) -> Path:
    df = df.copy()
    df["avl_event_time"] = (
        df.timestamp.dt.tz_convert(None).dt.strftime("%Y-%m-%d %H:%M:%S.%f")
    )
    out_df = pd.DataFrame({
        "id": df.entity_id.fillna("") + "_" + df.timestamp.astype(str),
        "bus_id": df.vehicle_id,
        "avl_event_time": df.avl_event_time,
        "bt_ver": "",
        "route_id": df.route_id,
        "pattern_id": PATTERN,
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
    out.parent.mkdir(parents=True, exist_ok=True)
    out_df.to_csv(out, index=False)
    return out


def departure_t_seconds(r) -> float:
    """Last stationary ping (≤ 0.03 mi forward progress so far) before the bus
    first makes >= 0.03 mi forward progress. Returns t in seconds (relative to
    r.t[0]). Falls back to r.t[0] if the trip never satisfies the threshold."""
    THRESH_M = 0.03 * M_PER_MI
    f = r.smoothed.f
    ts = r.t
    xs = f(ts)
    x0 = xs[0]
    progress = xs - x0
    moving = np.where(progress >= THRESH_M)[0]
    if len(moving) == 0:
        return ts[0]
    first_move = moving[0]
    # Last index BEFORE first_move that was still stationary; if first_move==0,
    # use t[0].
    return ts[max(0, first_move - 1)]


def plot_clock(recons: dict, out_path: Path) -> None:
    fig, ax = plt.subplots(figsize=(14, 8), dpi=160)
    ax.set_facecolor("#fafbfc")
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.set_title(f"{len(recons)} consecutive Route 22 SB trips — clock time",
                 fontsize=13, pad=8)
    ax.set_xlabel("Time of day (UTC)", fontsize=12)
    ax.set_ylabel("Distance along route (mi)", fontsize=12)
    ax.grid(True, alpha=0.3, linewidth=0.5)

    cmap = plt.cm.viridis
    items = sorted(recons.items(), key=lambda kv: kv[1].t[0])
    for i, (tid, r) in enumerate(items):
        ts = np.linspace(r.t[0], r.t[-1], 1500)
        xs = r.smoothed.f(ts) / M_PER_MI
        first_ts = pd.Timestamp(r.meta.first_ping)
        clock = first_ts + pd.to_timedelta(ts - r.t[0], unit="s")
        c = cmap(i / max(1, len(items) - 1))
        ax.plot(clock, xs, color=c, linewidth=1.0, alpha=0.85)

    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(out_path, dpi=160, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  saved: {out_path}")


def plot_aligned(recons: dict, out_path: Path) -> None:
    fig, ax = plt.subplots(figsize=(14, 8), dpi=160)
    ax.set_facecolor("#fafbfc")
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.set_title(
        f"{len(recons)} consecutive Route 22 SB trips — aligned to actual departure\n"
        f"(t=0 = last stationary ping before ≥ 0.03 mi forward progress)",
        fontsize=12, pad=8,
    )
    ax.set_xlabel("Minutes since departure", fontsize=12)
    ax.set_ylabel("Distance along route (mi)", fontsize=12)
    ax.grid(True, alpha=0.3, linewidth=0.5)

    cmap = plt.cm.viridis
    items = sorted(recons.items(), key=lambda kv: kv[1].t[0])
    for i, (tid, r) in enumerate(items):
        t0 = departure_t_seconds(r)
        ts = np.linspace(r.t[0], r.t[-1], 1500)
        xs_mi = r.smoothed.f(ts) / M_PER_MI
        x0_mi = float(r.smoothed.f(t0)) / M_PER_MI
        minutes = (ts - t0) / 60.0
        keep = minutes >= 0
        c = cmap(i / max(1, len(items) - 1))
        ax.plot(minutes[keep], xs_mi[keep] - x0_mi, color=c, linewidth=1.0, alpha=0.85)

    ax.set_xlim(left=0)
    ax.set_ylim(bottom=-0.05)
    fig.tight_layout()
    fig.savefig(out_path, dpi=160, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  saved: {out_path}")


def main() -> None:
    SLIDES.mkdir(exist_ok=True)
    df = load_pings_from(START_UTC)
    sb = select_first_n_sb_trips(df, START_UTC, N_TRIPS)
    print(f"Selected {sb.trip_id.nunique()} trips, {len(sb):,} pings")
    to_avl_csv(sb, CSV_OUT)
    print(f"Wrote: {CSV_OUT}")

    print(f"Reconstructing with bandwidth={BW}…")
    recons = reconstruct_csv(
        csv_path=CSV_OUT,
        gtfs_zip_path=GTFS,
        route_id=ROUTE,
        pattern_id=PATTERN,
        bandwidth=BW,
    )
    print(f"Reconstructed {len(recons)} trips")

    plot_clock(recons, SLIDES / "C1_50trips_clock.png")
    plot_aligned(recons, SLIDES / "C2_50trips_aligned.png")


if __name__ == "__main__":
    main()
