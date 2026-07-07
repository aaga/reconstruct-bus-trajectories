"""Render delay-decomposition figures.

Reads the aggregate.csv produced by ``run_decomposition.py`` and the per-trip
JSON for trip 1001350 to produce three figures:

  - ``figures/F1_trip_1001350.png``: stacked-bar per segment for one trip
  - ``figures/F2_corridor.png``: mean per-segment decomposition across
    all decomposed trips (with near-side hatching)
  - ``figures/F3_sources.png``: total minutes of each delay category
    summed across the corridor
  - ``figures/F4_near_side_stops.png``: list of near-side flagged stops
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.patches import Patch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
import corridor  # noqa: E402 -- centralized study-corridor constants

from core.decompose import build_segments_for_pattern  # noqa: E402

PATTERN_ID = corridor.PATTERN_ID
INTERSECTIONS_JSON = REPO / corridor.INTERSECTIONS_FILE
GTFS_ZIP = REPO / "data" / "gtfs" / "cta_gtfs.zip"
OUT_DIR = REPO / "outputs" / "out_decomposition"
FIG_DIR = REPO / "figures"
# Trip_ids in the bundle carry _<vehicle_id>_<chicago_date> suffixes to
# disambiguate BusTime trip_id reuse across days; the canonical "trip 1001350"
# is the original vehicle 4017 / 2026-05-05 instance.
TARGET_TRIP_ID = corridor.REFERENCE_TRIP_ID
TARGET_TRIP_LABEL = corridor.REFERENCE_TRIP_LABEL  # short label for titles + filenames

# Colors per delay component.
COL_T_FF = "#a8d4a6"             # muted green
COL_T_DWELL = "#7a9cc0"          # muted blue
COL_SIGNAL_UNIFORM = "#e5896a"   # warm orange
COL_SIGNAL_OVERFLOW = "#a8492a"  # darker orange/red
COL_D_CROSSING = "#d6b56a"       # mustard
COL_D_CONG = "#b27ab2"           # soft purple
COL_D_CONG_NEG = "#888888"       # grey for negative residual


def _x_centers(segments) -> np.ndarray:
    """Midpoint of each segment along route (in miles)."""
    return np.array([(s.x_start_m + s.x_end_m) / 2 / 1609.344 for s in segments])


def _seg_widths_mi(segments) -> np.ndarray:
    return np.array([s.length_m / 1609.344 for s in segments])


def figure_trip_waterfall(trip_data: dict, segments, out_path: Path) -> None:
    """Stacked bar per segment for a single trip."""
    seg_id_to_idx = {s.seg_id: i for i, s in enumerate(segments)}
    n = len(segments)
    rows = trip_data["segments"]
    by_idx = [None] * n
    for r in rows:
        i = seg_id_to_idx.get(r["seg_id"])
        if i is not None:
            by_idx[i] = r

    x_mi = _x_centers(segments)
    widths = _seg_widths_mi(segments) * 0.92

    # Build stacked bars.
    t_ff = np.array([(r["t_ff"] if r else 0) / 60 for r in by_idx])
    t_dwell = np.array([(r["t_dwell"] if r else 0) / 60 for r in by_idx])
    t_dwell_near = np.array([(r["t_dwell_near_signal"] if r else 0) / 60 for r in by_idx])
    d_sig_unif = np.array(
        [(r.get("d_signal_uniform", r.get("d_signal", 0)) if r else 0) / 60 for r in by_idx]
    )
    d_sig_ovrf = np.array(
        [(r.get("d_signal_overflow", 0) if r else 0) / 60 for r in by_idx]
    )
    d_crossing = np.array([(r["d_crossing"] if r else 0) / 60 for r in by_idx])
    d_cong = np.array([(r["d_congestion"] if r else 0) / 60 for r in by_idx])

    fig, ax = plt.subplots(figsize=(16, 7))
    bottom = np.zeros(n)

    ax.bar(x_mi, t_ff, width=widths, bottom=bottom, color=COL_T_FF,
           edgecolor="white", linewidth=0.4, label="Free-flow (T_ff)")
    bottom += t_ff

    # Dwell: hatched fraction for near-side stops
    t_dwell_clean = np.clip(t_dwell - t_dwell_near, 0, None)
    ax.bar(x_mi, t_dwell_clean, width=widths, bottom=bottom, color=COL_T_DWELL,
           edgecolor="white", linewidth=0.4, label="Dwell (T_dwell)")
    bottom += t_dwell_clean
    ax.bar(x_mi, t_dwell_near, width=widths, bottom=bottom, color=COL_T_DWELL,
           edgecolor="white", linewidth=0.4, hatch="///",
           label="Dwell @ near-side (ambiguous)")
    bottom += t_dwell_near

    ax.bar(x_mi, d_sig_unif, width=widths, bottom=bottom,
           color=COL_SIGNAL_UNIFORM, edgecolor="white", linewidth=0.4,
           label="Signal uniform")
    bottom += d_sig_unif
    ax.bar(x_mi, d_sig_ovrf, width=widths, bottom=bottom,
           color=COL_SIGNAL_OVERFLOW, edgecolor="white", linewidth=0.4,
           label="Signal overflow")
    bottom += d_sig_ovrf
    ax.bar(x_mi, d_crossing, width=widths, bottom=bottom, color=COL_D_CROSSING,
           edgecolor="white", linewidth=0.4, label="Crossing (D_crossing)")
    bottom += d_crossing

    # Congestion: positive = on top; negative = downward bar at the floor.
    pos = np.clip(d_cong, 0, None)
    neg = np.clip(d_cong, None, 0)
    ax.bar(x_mi, pos, width=widths, bottom=bottom, color=COL_D_CONG,
           edgecolor="white", linewidth=0.4, label="Congestion (D_congestion)")
    if neg.any():
        ax.bar(x_mi, neg, width=widths, bottom=0, color=COL_D_CONG_NEG,
               edgecolor="white", linewidth=0.4, alpha=0.55,
               label="Negative residual")

    ax.set_xlabel("Distance along Route 22 SB (mi)", fontsize=11)
    ax.set_ylabel("Time (minutes)", fontsize=11)
    ax.set_title(
        f"Delay decomposition — Trip {TARGET_TRIP_LABEL} "
        f"({trip_data['trip_id']}) (Route 22 SB)\n"
        f"Per signal-to-signal segment; T_obs = T_ff + T_dwell + D_signal "
        f"+ D_crossing + D_congestion",
        fontsize=12,
    )
    ax.grid(True, axis="y", alpha=0.25, linewidth=0.5)
    ax.set_xlim(0, max(x_mi) + 0.4)
    ax.legend(fontsize=9, loc="upper left", framealpha=0.95)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=200, bbox_inches="tight")
    plt.close(fig)
    print(f"Wrote {out_path}")


def figure_corridor_aggregate(agg_df: pd.DataFrame, segments, out_path: Path) -> None:
    seg_id_to_idx = {s.seg_id: i for i, s in enumerate(segments)}
    n = len(segments)
    ordered = [None] * n
    for _, row in agg_df.iterrows():
        i = seg_id_to_idx.get(row["seg_id"])
        if i is not None:
            ordered[i] = row

    x_mi = _x_centers(segments)
    widths = _seg_widths_mi(segments) * 0.92

    def col(key):
        return np.array([(r[f"mean_{key}"] if r is not None else 0) / 60
                         for r in ordered])

    t_ff = col("t_ff")
    t_dwell = col("t_dwell")
    t_dwell_near = col("t_dwell_near_signal")
    d_sig_unif = col("d_signal_uniform")
    d_sig_ovrf = col("d_signal_overflow")
    d_crossing = col("d_crossing")
    d_cong = col("d_congestion")

    fig, ax = plt.subplots(figsize=(16, 7))
    bottom = np.zeros(n)
    ax.bar(x_mi, t_ff, width=widths, bottom=bottom, color=COL_T_FF,
           edgecolor="white", linewidth=0.3, label="Free-flow (T_ff)")
    bottom += t_ff
    t_dwell_clean = np.clip(t_dwell - t_dwell_near, 0, None)
    ax.bar(x_mi, t_dwell_clean, width=widths, bottom=bottom, color=COL_T_DWELL,
           edgecolor="white", linewidth=0.3, label="Dwell (T_dwell)")
    bottom += t_dwell_clean
    ax.bar(x_mi, t_dwell_near, width=widths, bottom=bottom, color=COL_T_DWELL,
           edgecolor="white", linewidth=0.3, hatch="///",
           label="Dwell @ near-side (ambiguous)")
    bottom += t_dwell_near
    ax.bar(x_mi, d_sig_unif, width=widths, bottom=bottom,
           color=COL_SIGNAL_UNIFORM, edgecolor="white", linewidth=0.3,
           label="Signal uniform")
    bottom += d_sig_unif
    ax.bar(x_mi, d_sig_ovrf, width=widths, bottom=bottom,
           color=COL_SIGNAL_OVERFLOW, edgecolor="white", linewidth=0.3,
           label="Signal overflow")
    bottom += d_sig_ovrf
    ax.bar(x_mi, d_crossing, width=widths, bottom=bottom, color=COL_D_CROSSING,
           edgecolor="white", linewidth=0.3, label="Crossing (D_crossing)")
    bottom += d_crossing
    pos = np.clip(d_cong, 0, None)
    neg = np.clip(d_cong, None, 0)
    ax.bar(x_mi, pos, width=widths, bottom=bottom, color=COL_D_CONG,
           edgecolor="white", linewidth=0.3, label="Congestion (D_congestion)")
    if neg.any():
        ax.bar(x_mi, neg, width=widths, bottom=0, color=COL_D_CONG_NEG,
               edgecolor="white", linewidth=0.3, alpha=0.55,
               label="Negative residual")

    n_trips = int(agg_df.n_trips.iloc[0]) if len(agg_df) else 0
    ax.set_xlabel("Distance along Route 22 SB (mi)", fontsize=11)
    ax.set_ylabel("Mean time per trip (minutes)", fontsize=11)
    ax.set_title(
        f"Delay decomposition — corridor aggregate — Route 22 SB, mean of {n_trips} daytime trip(s)\n"
        f"Per signal-to-signal segment; bars stack to mean T_obs per segment",
        fontsize=12,
    )
    ax.grid(True, axis="y", alpha=0.25, linewidth=0.5)
    ax.set_xlim(0, max(x_mi) + 0.4)
    ax.legend(fontsize=9, loc="upper left", framealpha=0.95)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=200, bbox_inches="tight")
    plt.close(fig)
    print(f"Wrote {out_path}")


def figure_sources(agg_df: pd.DataFrame, out_path: Path) -> None:
    """Total minutes of each delay category summed across the corridor.

    The Dwell bar is split into two stacked segments: the clean (far-side
    / mid-block) portion as a solid fill, and the near-side ambiguous
    portion as a hatched overlay in the same color.
    """
    dwell_total_min = agg_df.mean_t_dwell.sum() / 60
    near_side_min = agg_df.mean_t_dwell_near_signal.sum() / 60
    dwell_clean_min = max(dwell_total_min - near_side_min, 0.0)

    # Top-to-bottom order (rendered reversed for barh).
    labels = ["Free-flow", "Dwell", "Signal uniform", "Signal overflow",
              "Crossing", "Congestion"]
    values = [
        agg_df.mean_t_ff.sum() / 60,
        dwell_total_min,
        agg_df.mean_d_signal_uniform.sum() / 60,
        agg_df.mean_d_signal_overflow.sum() / 60,
        agg_df.mean_d_crossing.sum() / 60,
        agg_df.mean_d_congestion.sum() / 60,
    ]
    colors = [COL_T_FF, COL_T_DWELL, COL_SIGNAL_UNIFORM,
              COL_SIGNAL_OVERFLOW, COL_D_CROSSING, COL_D_CONG]

    fig, ax = plt.subplots(figsize=(10, 5.5))
    y_positions = np.arange(len(labels))[::-1]
    rev_labels = labels[::-1]
    rev_values = values[::-1]
    rev_colors = colors[::-1]

    for y, lbl, val, col in zip(y_positions, rev_labels, rev_values, rev_colors):
        if lbl == "Dwell":
            ax.barh(y, dwell_clean_min, color=col, edgecolor="#333",
                    linewidth=0.4)
            ax.barh(y, near_side_min, left=dwell_clean_min, color=col,
                    edgecolor="#333", linewidth=0.4, hatch="///")
        else:
            ax.barh(y, val, color=col, edgecolor="#333", linewidth=0.4)
        ax.text(val + 0.3, y, f"{val:.1f} min", va="center", fontsize=10)

    ax.set_yticks(y_positions)
    ax.set_yticklabels(rev_labels)

    near_side_patch = Patch(facecolor=COL_T_DWELL, edgecolor="#333",
                            linewidth=0.4, hatch="///",
                            label=f"Hatched: near-side dwell "
                                  f"(ambiguous, {near_side_min:.1f} min)")
    ax.legend(handles=[near_side_patch], loc="lower right", fontsize=9,
              frameon=True)

    ax.set_xlabel("Mean minutes per trip, summed across the corridor",
                  fontsize=11)
    n_trips = int(agg_df.n_trips.iloc[0])
    total_obs = agg_df.mean_t_obs.sum() / 60
    ax.set_title(
        f"Delay sources — Route 22 SB corridor\n"
        f"Mean of {n_trips} daytime trip(s); observed run = {total_obs:.1f} min",
        fontsize=12,
    )
    ax.grid(True, axis="x", alpha=0.25, linewidth=0.5)
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=200, bbox_inches="tight")
    plt.close(fig)
    print(f"Wrote {out_path}")


def figure_near_side_stops(segments, out_path: Path) -> None:
    rows = []
    for s in segments:
        for stop in s.stops:
            if stop.is_near_side:
                rows.append((stop.dist_along_m / 1609.344, stop.stop_id, stop.name,
                              s.downstream_signal.intersection_node_id))
    if not rows:
        print("No near-side stops to render")
        return
    fig, ax = plt.subplots(figsize=(10, 0.4 * len(rows) + 1.5))
    ax.axis("off")
    cell_text = [[f"{r[0]:.2f}", r[1], r[2][:50], str(r[3])] for r in rows]
    table = ax.table(
        cellText=cell_text,
        colLabels=["mile", "stop_id", "stop name", "downstream signal node"],
        loc="center",
        cellLoc="left",
    )
    table.auto_set_font_size(False)
    table.set_fontsize(9)
    table.scale(1, 1.4)
    ax.set_title(
        f"Near-side stops on Route 22 SB (within 30 m upstream of a signal) — "
        f"{len(rows)} stops",
        fontsize=11,
    )
    fig.tight_layout()
    fig.savefig(out_path, dpi=200, bbox_inches="tight")
    plt.close(fig)
    print(f"Wrote {out_path}")


def main() -> int:
    segments = build_segments_for_pattern(PATTERN_ID, INTERSECTIONS_JSON, GTFS_ZIP)
    agg_df = pd.read_csv(OUT_DIR / "aggregate.csv")
    trip_data = json.loads((OUT_DIR / f"trip_{TARGET_TRIP_ID}.json").read_text())

    figure_trip_waterfall(trip_data, segments, FIG_DIR / f"F1_trip_{TARGET_TRIP_LABEL}.png")
    figure_corridor_aggregate(agg_df, segments, FIG_DIR / "F2_corridor.png")
    figure_sources(agg_df, FIG_DIR / "F3_sources.png")
    figure_near_side_stops(segments, FIG_DIR / "F4_near_side_stops.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
