"""Plot every Route 22 SB trip on pattern 3936 in the R2 archive,
aligned to actual departure.

Filters:
  - Auto-truncate each trip at first ping where dist_along ≥ shape_length-TERM_TOL_M.
  - Drop trips whose truncated ping series has any inter-ping gap > GAP_MAX_S.
  - Drop trips that don't actually reach the terminal, that have <MIN_PINGS
    after truncation, or that don't start near the origin.

Output: slides/F4_timespace_alltrips_aligned.png
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

from bus_trajectories.io import load_gtfs_shape_with_dist  # noqa: E402
from bus_trajectories.mapmatch.shape_snap import SnapToShapeMatcher  # noqa: E402
from bus_trajectories.pipeline import reconstruct_csv  # noqa: E402

R2 = "https://pub-777d0904efb449dc838791645b9e2e0f.r2.dev"
GTFS = "cta_gtfs.zip"
PATTERN = "3936"
ROUTE = "22"
SHAPE_ID = "67803936"
M_PER_MI = 1609.344
BW = 5
CACHE = Path("r2_cache")
CSV_OUT = Path("r2_route22_sb_all.csv")
SLIDES = Path("figures")

TERM_TOL_M = 50.0     # within 50 m of shape end = "at terminal"
ORIGIN_TOL_M = 600.0  # within 600 m of shape start at first ping
MIN_PINGS = 30
GAP_MAX_S = 300       # 5 minutes


def fetch(url: str, dst: Path) -> Path:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not dst.exists() or dst.stat().st_size == 0:
        subprocess.check_call(["curl", "-sSL", "-o", str(dst), url])
    return dst


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


def select_and_truncate(df: pd.DataFrame, matcher: SnapToShapeMatcher,
                         shape_len_m: float) -> pd.DataFrame:
    """Return the per-ping dataframe of all kept trips, truncated at terminal."""
    r22 = df[df.route_id == "22"].copy()
    r22 = r22.drop_duplicates(["vehicle_id", "timestamp"]).sort_values(
        ["trip_id", "timestamp"]
    ).reset_index(drop=True)

    # Disambiguate by date so trip_ids don't collide across days.
    r22["trip_uid"] = (
        r22.timestamp.dt.tz_convert("UTC").dt.strftime("%Y%m%d")
        + "_" + r22.trip_id.astype(str)
    )

    n_groups = r22.trip_uid.nunique()
    print(f"Candidate trips (route 22, all directions): {n_groups}")

    kept_parts = []
    stats = {"too_few_pings": 0, "not_starting_at_origin": 0,
             "no_terminal_reach": 0, "gap_too_long": 0, "kept": 0}

    for uid, g in r22.groupby("trip_uid", sort=False):
        if len(g) < MIN_PINGS:
            stats["too_few_pings"] += 1
            continue

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

        # Gap check (truncated series only).
        gaps_s = trunc.timestamp.diff().dt.total_seconds().fillna(0).to_numpy()
        if (gaps_s > GAP_MAX_S).any():
            stats["gap_too_long"] += 1
            continue

        kept_parts.append(trunc)
        stats["kept"] += 1

    print("Filter outcomes:")
    for k, v in stats.items():
        print(f"  {k}: {v}")

    if not kept_parts:
        raise SystemExit("no trips passed all filters")
    out = pd.concat(kept_parts, ignore_index=True)
    print(f"Kept {stats['kept']} trips, {len(out):,} pings (truncated to terminal)")
    return out


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
        f"All {len(recons)} Route 22 SB trips in the R2 archive — "
        f"aligned to actual departure\n"
        f"(t=0 = last stationary ping before ≥ 0.03 mi forward progress; "
        f"trips with >5 min gaps excluded; truncated at terminal)",
        fontsize=11, pad=8,
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
        ax.plot(minutes[keep], xs_mi[keep] - x0_mi, color=c, linewidth=0.6, alpha=0.5)

    ax.set_xlim(left=0)
    ax.set_ylim(bottom=-0.05)
    fig.tight_layout()
    fig.savefig(out_path, dpi=160, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  saved: {out_path}")


def main() -> None:
    SLIDES.mkdir(exist_ok=True)
    poly, dist_along = load_gtfs_shape_with_dist(GTFS, SHAPE_ID)
    shape_len_m = float(dist_along[-1])
    print(f"Shape {SHAPE_ID} length: {shape_len_m / M_PER_MI:.2f} mi")
    matcher = SnapToShapeMatcher(
        polyline_latlon=poly, dist_along_m_per_vertex=dist_along, max_perp_m=80.0,
    )

    df = load_all_cta()
    print(f"Total pings: {len(df):,}")

    sb_truncated = select_and_truncate(df, matcher, shape_len_m)
    to_avl_csv(sb_truncated, CSV_OUT)
    print(f"Wrote: {CSV_OUT}  ({CSV_OUT.stat().st_size:,} bytes, {len(sb_truncated):,} rows)")

    print(f"Reconstructing with bandwidth={BW}…")
    recons = reconstruct_csv(
        csv_path=CSV_OUT,
        gtfs_zip_path=GTFS,
        route_id=ROUTE,
        pattern_id=PATTERN,
        bandwidth=BW,
    )
    print(f"Reconstructed {len(recons)} trips")

    plot_aligned(recons, SLIDES / "F4_timespace_alltrips_aligned.png")


if __name__ == "__main__":
    main()
