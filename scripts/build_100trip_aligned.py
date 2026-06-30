"""Random sample 100 Route 22 SB trips on pattern 3936 from across the whole
R2 archive (~9 days of data), smooth them, and plot one aligned-departure
time-space diagram.

Output: slides/F3_timespace_100trips_aligned.png

"Departure" is the last stationary ping before the bus first makes ≥ 0.03 mi
of forward progress along the shape, matching the existing
timespace_route22_aligned_departure.png reference.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pyarrow.parquet as pq

PYTHONPATH_SRC = str((Path(__file__).resolve().parent.parent / "src"))
if PYTHONPATH_SRC not in sys.path:
    sys.path.insert(0, PYTHONPATH_SRC)

from bus_trajectories.pipeline import reconstruct_csv  # noqa: E402
from bus_trajectories.r2 import R2_PUB as R2, fetch  # noqa: E402

GTFS = "data/gtfs/cta_gtfs.zip"
PATTERN = "3936"
ROUTE = "22"
M_PER_MI = 1609.344
BW = 5
N_SAMPLE = 100
SEED = 42
CACHE = Path("caches/r2_cache")
CSV_OUT = Path("data/r2_route22_sb_100rand.csv")
SLIDES = Path("figures")



def load_all_cta() -> pd.DataFrame:
    fetch(f"{R2}/_manifest.parquet", CACHE / "_manifest.parquet")
    m = pq.read_table(CACHE / "_manifest.parquet").to_pandas()
    cta = m[m.agency == "cta"].sort_values(["year", "month", "day", "hour"])
    print(f"Loading {len(cta)} CTA hour-files…")
    parts = []
    for _, row in cta.iterrows():
        local = CACHE / row.path.replace("/", "__")
        fetch(f"{R2}/{row.path}", local)
        parts.append(pq.ParquetFile(local).read().to_pandas())
    return pd.concat(parts, ignore_index=True)


def select_sb_trips(df: pd.DataFrame) -> pd.DataFrame:
    """Filter to Route 22 SB, completed, full-length, sane duration.

    Trip key includes the date (UTC) of the first ping, since CTA reuses
    trip_id across days. The returned frame has a `trip_uid` column.
    """
    r22 = df[df.route_id == "22"].copy()
    r22 = r22.drop_duplicates(["vehicle_id", "timestamp"]).sort_values(
        ["trip_id", "timestamp"]
    ).reset_index(drop=True)

    # Disambiguate trip_id by date so different days' trips don't collide.
    r22["trip_uid"] = (
        r22.timestamp.dt.tz_convert("UTC").dt.strftime("%Y%m%d")
        + "_" + r22.trip_id.astype(str)
    )

    MIN_PINGS = 30
    MIN_LAT_SPAN = 0.12
    MAX_DURATION_MIN = 180

    rows = []
    for uid, g in r22.groupby("trip_uid"):
        if len(g) < MIN_PINGS:
            continue
        lf, ll = g.latitude.iloc[0], g.latitude.iloc[-1]
        if ll >= lf:
            continue
        if abs(lf - ll) < MIN_LAT_SPAN:
            continue
        dur_min = (g.timestamp.iloc[-1] - g.timestamp.iloc[0]).total_seconds() / 60.0
        if dur_min > MAX_DURATION_MIN:
            continue
        rows.append({"trip_uid": uid, "first_ts": g.timestamp.iloc[0], "n": len(g)})

    summary = pd.DataFrame(rows).sort_values("first_ts").reset_index(drop=True)
    print(f"Eligible SB trips across the archive: {len(summary)}")
    return r22[r22.trip_uid.isin(summary.trip_uid)].copy()


def sample_n(df: pd.DataFrame, n: int, seed: int) -> pd.DataFrame:
    uids = sorted(df.trip_uid.unique())
    rng = np.random.default_rng(seed)
    take = rng.choice(uids, size=min(n, len(uids)), replace=False)
    print(f"Sampled {len(take)} trips (seed={seed})")
    return df[df.trip_uid.isin(take)].copy()


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
        # Use trip_uid as the trip_id so reconstruct_csv treats each
        # (date, trip_id) as a separate trip.
        "trip_id": df.trip_uid,
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
    THRESH_M = 0.03 * M_PER_MI
    f = r.smoothed.f
    ts = r.t
    xs = f(ts)
    progress = xs - xs[0]
    moving = np.where(progress >= THRESH_M)[0]
    if len(moving) == 0:
        return ts[0]
    first_move = moving[0]
    return ts[max(0, first_move - 1)]


def plot_aligned(recons: dict, out_path: Path) -> None:
    fig, ax = plt.subplots(figsize=(14, 8), dpi=160)
    ax.set_facecolor("#fafbfc")
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.set_title(
        f"{len(recons)} random Route 22 SB trips from the R2 archive — "
        f"aligned to actual departure\n"
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
        ax.plot(minutes[keep], xs_mi[keep] - x0_mi, color=c, linewidth=0.8, alpha=0.7)

    ax.set_xlim(left=0)
    ax.set_ylim(bottom=-0.05)
    fig.tight_layout()
    fig.savefig(out_path, dpi=160, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  saved: {out_path}")


def main() -> None:
    SLIDES.mkdir(exist_ok=True)
    df = load_all_cta()
    print(f"Total pings: {len(df):,}")
    sb = select_sb_trips(df)
    sample = sample_n(sb, N_SAMPLE, SEED)
    to_avl_csv(sample, CSV_OUT)
    print(f"Wrote: {CSV_OUT}  ({CSV_OUT.stat().st_size:,} bytes, {len(sample):,} rows)")

    print(f"Reconstructing with bandwidth={BW}…")
    recons = reconstruct_csv(
        csv_path=CSV_OUT,
        gtfs_zip_path=GTFS,
        route_id=ROUTE,
        pattern_id=PATTERN,
        bandwidth=BW,
    )
    print(f"Reconstructed {len(recons)} trips")

    plot_aligned(recons, SLIDES / "F3_timespace_100trips_aligned.png")


if __name__ == "__main__":
    main()
