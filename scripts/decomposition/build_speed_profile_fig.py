"""Speed profile figure for a chosen distance window on one trip.

Renders four stacked panels:
  * Top strip: numbered cards for each signal-to-signal segment, colored
    by sign of D_congestion (red = negative).
  * Speed vs distance (sharex with strip).
  * Speed vs time — different x scale, separate panel.
  * Tabular legend listing each segment's full decomposition numbers.

Default window is 2 miles centered on Diversey→Wrightwood for trip 1001350.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from bus_trajectories.delay_decomposition import (  # noqa: E402
    AbsoluteSpeedThreshold,
    ProximityDwellAttributor,
    build_segments_for_pattern,
    detect_events,
)
from bus_trajectories.delay_decomposition.attribution import (  # noqa: E402
    EventAttribution, attribute_event,
)
from bus_trajectories.delay_decomposition.decompose import (  # noqa: E402
    _apply_overflow_pass, _evaluate_dense,
)
from bus_trajectories.delay_decomposition.loss import loss_shoulders_for_event  # noqa: E402
from bus_trajectories.delay_decomposition.travel_time import _last_t_at_x  # noqa: E402
from bus_trajectories.serialize import load_records  # noqa: E402

PATTERN_ID = "3936"
INTERSECTIONS_JSON = REPO / "intersections_route22.json"
GTFS_ZIP = REPO / "cta_gtfs.zip"
DAYTIME_BUNDLE = REPO / "out_r2_bw5" / "trajectories.json"
DECOMP_DIR = REPO / "out_decomposition"
FIG_DIR = REPO / "figures"
TARGET_TRIP_ID = "1001350_4017_2026-05-05"
M_PER_MILE = 1609.344

COL_DWELL = "#7a9cc0"          # blue
COL_DWELL_NEAR = "#5276a3"     # darker blue
COL_SIGNAL_UNIFORM = "#e5896a"  # warm orange
COL_SIGNAL_OVERFLOW = "#a8492a"  # darker orange/red
COL_CROSSING = "#d6b56a"        # mustard
COL_SLOWDOWN = "#b27ab2"        # purple
COL_LOSS = "#cccccc"            # grey
COL_SEG_POS = "#dceedb"
COL_SEG_NEG = "#f6d0c8"


def _color_for(attr: EventAttribution) -> str:
    cat = attr.category
    if cat == "dwell":
        return COL_DWELL_NEAR if attr.dwell_near_signal else COL_DWELL
    if cat == "crossing":
        return COL_CROSSING
    if cat == "signal_uniform":
        return COL_SIGNAL_UNIFORM
    if cat == "signal_overflow":
        return COL_SIGNAL_OVERFLOW
    return COL_SLOWDOWN


def _primary_seg_by_time(segments, seg_bounds, ev):
    """Match decompose.py's rule: segment with the most clipped duration."""
    best_seg, best_dur = None, 0.0
    for s in segments:
        t_lo, t_hi = seg_bounds[s.seg_id]
        dur = max(0.0, min(t_hi, ev.t_end) - max(t_lo, ev.t_start))
        if dur > best_dur:
            best_dur, best_seg = dur, s
    return best_seg


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--x0", type=float, default=6.0, help="Window start (miles)")
    ap.add_argument("--x1", type=float, default=8.0, help="Window end (miles)")
    ap.add_argument("--trip-id", default=TARGET_TRIP_ID)
    ap.add_argument("--out", default=None)
    ap.add_argument("--show-loss-shoulders", action="store_true",
                    help="Render the accel/decel shoulder spans in grey.")
    args = ap.parse_args()

    x0_m, x1_m = args.x0 * M_PER_MILE, args.x1 * M_PER_MILE

    segments = build_segments_for_pattern(PATTERN_ID, INTERSECTIONS_JSON, GTFS_ZIP)
    rec = next(r for r in load_records(DAYTIME_BUNDLE) if r["trip_id"] == args.trip_id)
    f, t, x, v_mph = _evaluate_dense(rec, 2.0)
    all_events = detect_events(t, x, v_mph, AbsoluteSpeedThreshold(5.0))
    dwell_attributor = ProximityDwellAttributor()

    seg_bounds = {
        s.seg_id: (_last_t_at_x(f, s.x_start_m), _last_t_at_x(f, s.x_end_m))
        for s in segments
    }

    window_segs = [s for s in segments if s.x_end_m > x0_m and s.x_start_m < x1_m]
    decomp = json.loads((DECOMP_DIR / f"trip_{args.trip_id}.json").read_text())
    seg_decomp = {r["seg_id"]: r for r in decomp["segments"]}

    # Attribute every event for the trip (so overflow pass works correctly),
    # then keep just those visible in the window.
    full_records: list[tuple] = []
    loss_per_idx: dict[int, tuple] = {}
    for ev in all_events:
        primary = _primary_seg_by_time(segments, seg_bounds, ev)
        if primary is None:
            continue
        if args.show_loss_shoulders:
            decel_int, accel_int = loss_shoulders_for_event(t, v_mph, ev)
            total_loss = (decel_int[1] - decel_int[0]) + (accel_int[1] - accel_int[0])
        else:
            decel_int = (ev.t_start, ev.t_start)
            accel_int = (ev.t_end, ev.t_end)
            total_loss = 0.0
        attr = attribute_event(ev, primary, dwell_attributor, total_loss)
        full_records.append((ev, attr, primary))
        loss_per_idx[len(full_records) - 1] = (decel_int, accel_int)
    full_records = _apply_overflow_pass(full_records)

    # Filter to window AFTER overflow pass.
    visible = []
    for idx, (ev, attr, primary) in enumerate(full_records):
        if ev.x_end < x0_m - 100 or ev.x_start > x1_m + 100:
            continue
        visible.append((ev, attr, primary, loss_per_idx.get(idx)))

    # --- figure layout --------------------------------------------------
    n_segs = len(window_segs)
    table_h = max(2.0, 0.32 * n_segs + 0.8)
    fig = plt.figure(figsize=(16, 14 + table_h * 0.5))
    # Outer gridspec: keep the segment strip snug against the speed-distance
    # panel (they share x), but leave room below speed-distance so its xlabel
    # isn't clipped by the speed-time panel.
    gs_outer = fig.add_gridspec(
        3, 1,
        height_ratios=[0.35 + 2.0, 1.6, table_h * 0.6],
        hspace=0.32,
    )
    gs_top = gs_outer[0].subgridspec(
        2, 1, height_ratios=[0.35, 2.0], hspace=0.10,
    )
    ax_seg = fig.add_subplot(gs_top[0])
    ax_v_dist = fig.add_subplot(gs_top[1], sharex=ax_seg)
    ax_v_time = fig.add_subplot(gs_outer[1])  # NOT sharex — different scale
    ax_table = fig.add_subplot(gs_outer[2])

    # --- Top strip: numbered segment cards (sharex with speed-distance) ---
    ax_seg.set_xlim(args.x0, args.x1)
    ax_seg.set_ylim(0, 1)
    ax_seg.axis("off")
    for i, s in enumerate(window_segs, start=1):
        r = seg_decomp.get(s.seg_id, {})
        cong = r.get("d_congestion", 0.0)
        face = COL_SEG_NEG if cong < 0 else COL_SEG_POS
        x_lo, x_hi = s.x_start_m / M_PER_MILE, s.x_end_m / M_PER_MILE
        ax_seg.add_patch(mpatches.Rectangle(
            (x_lo, 0.12), x_hi - x_lo, 0.76,
            facecolor=face, edgecolor="#666", linewidth=0.7,
        ))
        ax_seg.text(
            (x_lo + x_hi) / 2, 0.5, f"{i}",
            ha="center", va="center", fontsize=12, fontweight="bold",
            color="#a00" if cong < 0 else "#222",
        )
    ax_seg.set_title(
        f"Trip {args.trip_id} — signal-to-signal segments, mile "
        f"{args.x0:.2f} → {args.x1:.2f}  (red = negative D_congestion; "
        f"full details in table below)",
        fontsize=11, loc="left", pad=4,
    )

    # --- Speed vs distance ---
    in_win = (x >= x0_m - 50) & (x <= x1_m + 50)
    x_mi = x[in_win] / M_PER_MILE
    v_in = v_mph[in_win]

    def x_at_time(tv: float) -> float:
        i = int(np.clip(np.searchsorted(t, tv), 0, len(t) - 1))
        return float(x[i])

    for ev, attr, _primary, loss in visible:
        color = _color_for(attr)
        x_lo = ev.x_start / M_PER_MILE
        x_hi = ev.x_end / M_PER_MILE
        ax_v_dist.axvspan(x_lo, x_hi, color=color, alpha=0.55, zorder=1)
        if loss is not None and args.show_loss_shoulders:
            for sh in loss:
                if sh[1] - sh[0] <= 0.5:
                    continue
                xl = x_at_time(sh[0]) / M_PER_MILE
                xh = x_at_time(sh[1]) / M_PER_MILE
                ax_v_dist.axvspan(xl, xh, color=COL_LOSS, alpha=0.55, zorder=0)
        if x_hi - x_lo >= 0.01 and args.x0 <= (x_lo + x_hi) / 2 <= args.x1:
            label = attr.category.split("_")[0] if "_" in attr.category else attr.category[:6]
            if attr.category == "signal_overflow":
                label = "overflow"
            elif attr.category == "signal_uniform":
                label = "signal"
            ax_v_dist.text(
                (x_lo + x_hi) / 2, 1.0,
                f"{label}\n{ev.duration_s:.0f}s",
                ha="center", va="bottom", fontsize=7, color="#222",
            )
    ax_v_dist.plot(x_mi, v_in, color="black", linewidth=1.6, zorder=3)
    ax_v_dist.axhline(5.0, color="#888", linestyle=":", linewidth=0.9, alpha=0.7)
    ax_v_dist.text(args.x0 + 0.01, 5.3, "5 mph (event threshold)",
                    fontsize=7.5, color="#666")
    ax_v_dist.set_ylabel("Speed (mph)", fontsize=11)
    ax_v_dist.set_xlabel("Distance along Route 22 SB (mi)", fontsize=10)
    ax_v_dist.set_ylim(-0.5, max(35, float(v_in.max()) + 2))
    ax_v_dist.grid(True, alpha=0.25, linewidth=0.5)

    # Stop markers along the distance panel.
    for s in window_segs:
        for stop in s.stops:
            xs = stop.dist_along_m / M_PER_MILE
            if not (args.x0 <= xs <= args.x1):
                continue
            ax_v_dist.scatter(
                [xs], [ax_v_dist.get_ylim()[0] + 0.5],
                marker="^", s=53,
                c=("crimson" if stop.is_near_side else "#444"),
                edgecolors="white", linewidths=0.6, zorder=4,
            )

    # Segment boundary verticals on the distance panels.
    boundary_set = {s.x_start_m for s in window_segs} | {s.x_end_m for s in window_segs}
    for bx in sorted(boundary_set):
        bx_mi = bx / M_PER_MILE
        for ax in (ax_v_dist, ax_seg):
            ax.axvline(bx_mi, color="#555", linestyle="--", linewidth=0.7,
                        alpha=0.7, zorder=2)

    # --- Speed vs time (separate x scale) ---
    # Determine the time window: from when the bus first reaches x0_m to
    # when it last reaches x1_m, padded a little.
    t_in = float(_last_t_at_x(f, x0_m))
    t_out = float(_last_t_at_x(f, x1_m))
    t_lo_win, t_hi_win = t_in - 10, t_out + 10
    in_t_win = (t >= t_lo_win) & (t <= t_hi_win)
    t_min_arr = (t[in_t_win] - t[0]) / 60.0
    v_t = v_mph[in_t_win]

    for ev, attr, _primary, loss in visible:
        color = _color_for(attr)
        t_lo_m = (ev.t_start - t[0]) / 60.0
        t_hi_m = (ev.t_end - t[0]) / 60.0
        ax_v_time.axvspan(t_lo_m, t_hi_m, color=color, alpha=0.55, zorder=1)
        if loss is not None and args.show_loss_shoulders:
            for sh in loss:
                if sh[1] - sh[0] <= 0.5:
                    continue
                ax_v_time.axvspan(
                    (sh[0] - t[0]) / 60.0, (sh[1] - t[0]) / 60.0,
                    color=COL_LOSS, alpha=0.55, zorder=0,
                )

    ax_v_time.plot(t_min_arr, v_t, color="black", linewidth=1.6, zorder=3)
    ax_v_time.axhline(5.0, color="#888", linestyle=":", linewidth=0.9, alpha=0.7)
    ax_v_time.set_xlabel("Time into trip (min)", fontsize=11)
    ax_v_time.set_ylabel("Speed (mph)", fontsize=11)
    ax_v_time.set_xlim((t_lo_win - t[0]) / 60.0, (t_hi_win - t[0]) / 60.0)
    ax_v_time.set_ylim(-0.5, max(35, float(v_t.max()) + 2))
    ax_v_time.grid(True, alpha=0.25, linewidth=0.5)

    # Segment time-boundary verticals.
    for s in window_segs:
        for tb in seg_bounds[s.seg_id]:
            ax_v_time.axvline(
                (tb - t[0]) / 60.0, color="#555", linestyle="--",
                linewidth=0.7, alpha=0.7, zorder=2,
            )

    # Legend (lower right of speed-distance panel).
    handles = [
        mpatches.Patch(color=COL_DWELL, alpha=0.55, label="Dwell"),
        mpatches.Patch(color=COL_DWELL_NEAR, alpha=0.55,
                        label="Dwell @ near-side"),
        mpatches.Patch(color=COL_CROSSING, alpha=0.55, label="Crossing"),
        mpatches.Patch(color=COL_SIGNAL_UNIFORM, alpha=0.55, label="Signal (uniform)"),
        mpatches.Patch(color=COL_SIGNAL_OVERFLOW, alpha=0.55, label="Signal (overflow)"),
        mpatches.Patch(color=COL_SLOWDOWN, alpha=0.55, label="Slowdown (→ D_cong)"),
    ]
    if args.show_loss_shoulders:
        handles.append(
            mpatches.Patch(color=COL_LOSS, alpha=0.55, label="Accel/decel shoulder")
        )
    handles.extend([
        plt.Line2D([0], [0], marker="^", color="white",
                   markerfacecolor="#444", markersize=8, label="Bus stop"),
        plt.Line2D([0], [0], marker="^", color="white",
                   markerfacecolor="crimson", markersize=8,
                   label="Near-side stop"),
    ])
    ax_v_dist.legend(handles=handles, fontsize=8.2, loc="upper right",
                      framealpha=0.95, ncol=2)
    ax_v_dist.set_xlim(args.x0, args.x1)

    # --- Tabular legend at bottom ---
    ax_table.axis("off")
    header = ["#", "seg_id (full)", "x range (mi)", "T_obs (s)",
              "T_ff (s)", "T_dwell (s)", "D_sig_unif (s)", "D_sig_ovrf (s)",
              "D_crossing (s)", "D_cong (s)", "stops (near-side)"]
    cong_col_idx = header.index("D_cong (s)")
    body, cell_colors = [], []
    for i, s in enumerate(window_segs, start=1):
        r = seg_decomp.get(s.seg_id, {})
        cong = r.get("d_congestion", 0.0)
        n_stops = len(s.stops)
        n_near = sum(1 for st in s.stops if st.is_near_side)
        stops_str = f"{n_stops} ({n_near} near)" if n_near else str(n_stops)
        body.append([
            str(i),
            s.seg_id,
            f"[{s.x_start_m / M_PER_MILE:.2f}, {s.x_end_m / M_PER_MILE:.2f}]",
            f"{r.get('t_obs',0):.0f}",
            f"{r.get('t_ff',0):.0f}",
            f"{r.get('t_dwell',0):.0f}",
            f"{r.get('d_signal_uniform',0):.0f}",
            f"{r.get('d_signal_overflow',0):.0f}",
            f"{r.get('d_crossing',0):.0f}",
            f"{cong:+.0f}",
            stops_str,
        ])
        row_color = COL_SEG_NEG if cong < 0 else COL_SEG_POS
        cell_colors.append([row_color] * len(header))
    table = ax_table.table(
        cellText=body, colLabels=header, cellColours=cell_colors,
        loc="center", cellLoc="center", colLoc="center",
    )
    table.auto_set_font_size(False)
    table.set_fontsize(9)
    table.scale(1, 1.35)
    for (r_idx, c_idx), cell in table.get_celld().items():
        if r_idx == 0:
            cell.set_facecolor("#ddd")
            cell.set_text_props(fontweight="bold")
        elif c_idx == 0:
            cong = float(body[r_idx - 1][cong_col_idx])
            cell.set_text_props(
                fontweight="bold",
                color="#a00" if cong < 0 else "#222",
            )

    out = Path(args.out) if args.out else FIG_DIR / f"decomp_speed_profile_{args.trip_id}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=200, bbox_inches="tight")
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
