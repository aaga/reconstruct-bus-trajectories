"""Delay attribution figures — per-trip (G_*) and corridor-aggregate
(H_*) views of where delay accumulates along Route 22 SB.

Replaces the naive ``scripts/naive_delay/build_naive_delay_slides.py``:
delay is attributed via the chapter-3 decomposition (dwell, crossing,
signal_uniform, signal_overflow, slowdown) rather than the naive
"slow-window split across nearby features" heuristic.

Outputs (all in ``figures/``):

  G1_waterfall_1001350.png   – per-trip 2-column table
  G2_bar_1001350.png                 – per-trip horizontal bar (top 25)
  H1_waterfall_aggregate.png – aggregate 2-column table
  H2_bar_aggregate.png               – aggregate horizontal bar, mean ± std
  H3_bar_aggregate_median.png        – aggregate horizontal bar, median + IQR
  H4_stem.png            – stem along route, mean
  H5_stem_median.png     – stem along route, median
  H6_map_bubbles.png                 – bubble map of facility delay
  H7_3d_map_stem.png                 – 3D stem over a basemap

Usage:
    PYTHONPATH=src uv run python scripts/decomposition/build_attribution_slides.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from core.decompose import (  # noqa: E402
    build_facility_index,
    build_segments_for_pattern,
    decompose_trip,
    per_facility_seconds,
)
from core.decompose.travel_time import (  # noqa: E402
    load_freeflow_table,
)
from dataio.gtfs import load_gtfs_shape_with_dist  # noqa: E402
from core.serialize import load_records  # noqa: E402

PATTERN_ID = "3936"
SHAPE_ID = "67803936"
GTFS = REPO / "data" / "gtfs" / "cta_gtfs.zip"
INTERSECTIONS_JSON = REPO / "intersections_route22.json"
BUNDLE = REPO / "outputs" / "out_r2_bw5" / "trajectories.json"
FF_TABLE_PATH = REPO / "outputs" / "out_decomposition" / "freeflow_segments.json"
FIG_DIR = REPO / "figures"
M_PER_MI = 1609.344

# Disambiguated trip-id for the canonical "trip 1001350" — vehicle 4017,
# 2026-05-05 Chicago.
TARGET_TRIP_ID = "1001350_4017_2026-05-05"
TARGET_TRIP_LABEL = "1001350"


# Facility-kind colors, labels and ordering (shared palette).
from viz.colors import (  # noqa: E402
    CONG_COLOR, KIND_COLOR, KIND_LABEL, KIND_ORDER, OTHER_COLOR,
)


# --------------------------------------------------------------------------
# Data layer
# --------------------------------------------------------------------------


def load_corridor_objects():
    """Load segments, freeflow table, the polyline shape (for lat/lon),
    and project lat/lon onto each FacilityMeta."""
    segments = build_segments_for_pattern(PATTERN_ID, INTERSECTIONS_JSON, GTFS)
    ff = load_freeflow_table(FF_TABLE_PATH)
    poly, dist_along = load_gtfs_shape_with_dist(GTFS, SHAPE_ID)
    facility_index = build_facility_index(segments)
    # Signals and crossings carry lat/lon from ControlPoint; stops don't —
    # project stops onto the polyline at their dist_along_m.
    for fid, ft in facility_index.items():
        if ft.lat is None or ft.lon is None:
            idx = int(np.argmin(np.abs(dist_along - ft.dist_m)))
            object.__setattr__(ft, "lat", float(poly[idx, 0]))
            object.__setattr__(ft, "lon", float(poly[idx, 1]))
    return segments, ff, facility_index, poly, dist_along


def attribute_trip(rec, segments, ff):
    """Run chapter-3 decomposition and re-aggregate per facility.

    Returns ``(sec_per, other_s, t_obs_s, cong_per_seg)`` where ``sec_per``
    is keyed by facility_id and ``cong_per_seg`` is keyed by seg_id
    (residual ``d_congestion`` in seconds, clipped to ≥ 0).
    """
    decomp = decompose_trip(rec, segments, ff, include_loss=False)
    sec_per, _, other_s = per_facility_seconds(decomp)
    cong_per_seg = {sd.seg_id: max(sd.d_congestion, 0.0) for sd in decomp.segments}
    return sec_per, other_s, decomp.t_obs_total, cong_per_seg


def _facility_rows(sec_per: dict[str, float], facility_index) -> list[dict]:
    """Materialize a list-of-rows view sorted by descending delay."""
    rows = []
    for fid, sec in sec_per.items():
        if sec <= 0:
            continue
        ft = facility_index.get(fid)
        if ft is None:
            continue
        rows.append({
            "facility_id": fid,
            "label": ft.label,
            "kind": ft.kind,
            "d_mi": ft.dist_m / M_PER_MI,
            "lat": ft.lat,
            "lon": ft.lon,
            "min": sec / 60,
        })
    rows.sort(key=lambda r: -r["min"])
    return rows


def compute_g_data(segments, ff, facility_index):
    rec = next(r for r in load_records(BUNDLE) if r["trip_id"] == TARGET_TRIP_ID)
    sec_per, other_s, t_obs_s, _ = attribute_trip(rec, segments, ff)
    return _facility_rows(sec_per, facility_index), other_s, t_obs_s


def compute_h_data(segments, ff, facility_index):
    """Decompose every trip in the bundle and accumulate per-facility seconds.

    Returns ``(rows, other_secs, n_trips, seg_rows)`` where ``rows`` is the
    per-facility aggregate, ``other_secs`` is the per-trip slowdown total,
    and ``seg_rows`` is the per-segment residual-congestion aggregate
    (positioned at the segment midpoint along the route).
    """
    records = list(load_records(BUNDLE))
    n_trips = len(records)
    print(f"  decomposing {n_trips} trips for H aggregate…")
    per_facility: dict[str, np.ndarray] = {
        fid: np.zeros(n_trips) for fid in facility_index
    }
    per_seg_cong: dict[str, np.ndarray] = {
        s.seg_id: np.zeros(n_trips) for s in segments
    }
    other_secs = np.zeros(n_trips)
    for i, rec in enumerate(records):
        try:
            sec_per, other_s, _, cong_per_seg = attribute_trip(rec, segments, ff)
        except Exception as exc:
            print(f"    [{i+1}/{n_trips}] skipping {rec.get('trip_id')}: {exc}")
            continue
        for fid, sec in sec_per.items():
            if fid in per_facility:
                per_facility[fid][i] = sec
        for sid, sec in cong_per_seg.items():
            if sid in per_seg_cong:
                per_seg_cong[sid][i] = sec
        other_secs[i] = other_s
        if (i + 1) % 200 == 0:
            print(f"    [{i+1}/{n_trips}]")

    rows = []
    for fid, arr in per_facility.items():
        n_with = int((arr > 0).sum())
        if n_with == 0:
            continue
        ft = facility_index[fid]
        q25, med, q75, p95 = np.percentile(arr, [25, 50, 75, 95]) / 60
        rows.append({
            "facility_id": fid,
            "label": ft.label,
            "kind": ft.kind,
            "d_mi": ft.dist_m / M_PER_MI,
            "lat": ft.lat,
            "lon": ft.lon,
            "total_min": arr.sum() / 60,
            "mean_min": arr.mean() / 60,
            "std_min": arr.std() / 60,
            "median_min": float(med),
            "q25_min": float(q25),
            "q75_min": float(q75),
            "p95_min": float(p95),
            "n": n_with,
        })
    rows.sort(key=lambda r: -r["mean_min"])

    seg_index = {s.seg_id: s for s in segments}
    seg_rows = []
    for sid, arr in per_seg_cong.items():
        n_with = int((arr > 0).sum())
        if n_with == 0:
            continue
        seg = seg_index[sid]
        d_mid_mi = (seg.x_start_m + seg.x_end_m) / 2 / M_PER_MI
        q25, med, q75, p95 = np.percentile(arr, [25, 50, 75, 95]) / 60
        seg_rows.append({
            "seg_id": sid,
            "d_mi": d_mid_mi,
            "x_start_mi": seg.x_start_m / M_PER_MI,
            "x_end_mi": seg.x_end_m / M_PER_MI,
            "mean_min": arr.mean() / 60,
            "median_min": float(med),
            "q25_min": float(q25),
            "q75_min": float(q75),
            "p95_min": float(p95),
            "n": n_with,
        })
    seg_rows.sort(key=lambda r: -r["mean_min"])
    return rows, other_secs, n_trips, seg_rows


# --------------------------------------------------------------------------
# Visual helpers
# --------------------------------------------------------------------------


def _wrap_label(label: str, max_chars: int = 38) -> str:
    return label if len(label) <= max_chars else label[: max_chars - 1] + "…"


def _color(kind: str) -> str:
    return KIND_COLOR.get(kind, "#888")


def _legend_handles():
    return (
        [plt.Rectangle((0, 0), 1, 1, color=KIND_COLOR[k]) for k in KIND_ORDER],
        [KIND_LABEL[k] for k in KIND_ORDER],
    )


def _render_table(rows, *, title, subtitle, columns, col_titles,
                  out: Path, n_cols: int = 2) -> None:
    n_rows = len(rows)
    per_col = (n_rows + n_cols - 1) // n_cols
    fig_h = max(9.0, 0.26 * per_col + 2.2)
    fig_w = 16.0
    fig, axes = plt.subplots(1, n_cols, figsize=(fig_w, fig_h), dpi=160)
    if n_cols == 1:
        axes = [axes]

    title_y = 1 - 0.6 / fig_h
    sub_y = 1 - 1.05 / fig_h
    fig.suptitle(title, fontsize=16, fontweight="bold", y=title_y)
    fig.text(0.5, sub_y, subtitle, ha="center", fontsize=10, color="#444")

    for ci, ax in enumerate(axes):
        ax.set_xticks([])
        ax.set_yticks([])
        for s in ("top", "right", "left", "bottom"):
            ax.spines[s].set_visible(False)
        chunk = rows[ci * per_col: (ci + 1) * per_col]
        if not chunk:
            continue

        header_y = 1.0
        ax.text(0.02, header_y, "feature", fontsize=9, fontweight="bold",
                transform=ax.transAxes, va="bottom")
        n_num = len(columns)
        right_edge = 0.99
        col_step = 0.085
        x_offsets = {
            col: right_edge - (n_num - 1 - i) * col_step
            for i, col in enumerate(columns)
        }
        for col, title_text in zip(columns, col_titles):
            ax.text(x_offsets[col], header_y, title_text, fontsize=9,
                    fontweight="bold", transform=ax.transAxes,
                    ha="right", va="bottom")
        ax.plot([0, 1], [header_y - 0.005, header_y - 0.005],
                color="#888", linewidth=0.6, transform=ax.transAxes,
                clip_on=False)

        for i, row in enumerate(chunk):
            y = header_y - 0.020 - (i + 0.5) * (1.0 / max(per_col, 1))
            color = _color(row["kind"])
            ax.add_patch(plt.Rectangle((0.005, y - 0.011), 0.012, 0.022,
                                        transform=ax.transAxes,
                                        facecolor=color, edgecolor="none",
                                        clip_on=False))
            leftmost = min(x_offsets.values())
            max_chars = max(20, int((leftmost - 0.025) * 70))
            label = row["label"]
            if len(label) > max_chars:
                label = label[: max_chars - 1] + "…"
            ax.text(0.025, y, label, fontsize=8.5, transform=ax.transAxes,
                    va="center")
            for col in columns:
                v = row[col]
                if isinstance(v, float) and np.isnan(v):
                    s = "—"
                else:
                    s = f"{v:.2f}"
                ax.text(x_offsets[col], y, s, fontsize=8.5,
                        transform=ax.transAxes, ha="right", va="center",
                        family="monospace")

    handles, labels = _legend_handles()
    x = 0.40
    for h, lab in zip(handles, labels):
        fig.text(x, 0.012, "■", color=h.get_facecolor(), fontsize=12,
                 ha="left", va="bottom")
        fig.text(x + 0.012, 0.012, lab, color="#444", fontsize=9,
                 ha="left", va="bottom")
        x += 0.08

    top_frac = 1 - 1.6 / fig_h
    fig.subplots_adjust(left=0.03, right=0.99, top=top_frac, bottom=0.05,
                       wspace=0.10)
    fig.savefig(out, dpi=160, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  saved: {out}")


# --------------------------------------------------------------------------
# G figures
# --------------------------------------------------------------------------


def slide_G_table(rows, other_s, t_obs_s) -> None:
    print("[G] per-trip attribution table…")
    total_attr_s = sum(r["min"] for r in rows) * 60
    trip_min = t_obs_s / 60
    _render_table(
        rows,
        title=f"Delay attribution — Trip {TARGET_TRIP_LABEL} ({TARGET_TRIP_ID})",
        subtitle=(
            f"Run: {trip_min:.1f} min  |  "
            f"Attributed to facility: {total_attr_s / 60:.1f} min "
            f"({total_attr_s / (t_obs_s) * 100:.0f}%)  |  "
            f"OTHER (slowdown / unattributed): {other_s / 60:.2f} min"
        ),
        columns=["d_mi", "min"],
        col_titles=["mile", "delay (min)"],
        out=FIG_DIR / f"G1_waterfall_{TARGET_TRIP_LABEL}.png",
        n_cols=2,
    )


def slide_G_bar(rows, other_s, t_obs_s) -> None:
    print("[G_bar] per-trip horizontal bar (top 25)…")
    TOP = 25
    top = rows[:TOP]
    fig, ax = plt.subplots(figsize=(14, 9), dpi=160)
    ax.set_facecolor("white")
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)

    labels = [_wrap_label(r["label"]) for r in top]
    values = [r["min"] for r in top]
    colors = [_color(r["kind"]) for r in top]
    y = np.arange(len(top))[::-1]
    ax.barh(y, values, color=colors, edgecolor="#333", linewidth=0.4,
            height=0.78)
    for yi, v in zip(y, values):
        ax.text(v + 0.04, yi, f"{v:.2f}", va="center", fontsize=8.5)
    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=9)
    ax.set_xlabel("Delay attributed (minutes)", fontsize=11)
    trip_min = t_obs_s / 60
    total_attr_min = sum(values)
    ax.set_title(
        f"Delay attribution — Trip {TARGET_TRIP_LABEL}, top {len(top)} "
        f"facilities ({TARGET_TRIP_ID})\n"
        f"Run: {trip_min:.1f} min  |  "
        f"Top-{TOP} attributed: {total_attr_min:.1f} min  |  "
        f"OTHER (slowdown): {other_s / 60:.2f} min",
        fontsize=12, pad=10,
    )
    ax.grid(True, axis="x", alpha=0.3, linewidth=0.5)
    handles, lbls = _legend_handles()
    ax.legend(handles, lbls, loc="lower right", fontsize=9, frameon=True)
    fig.tight_layout()
    out = FIG_DIR / f"G2_bar_{TARGET_TRIP_LABEL}.png"
    fig.savefig(out, dpi=160, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  saved: {out}")


def slide_G_stem(rows, other_s, t_obs_s) -> None:
    print(f"[G_stem] stem along route for trip {TARGET_TRIP_LABEL}…")
    trip_min = t_obs_s / 60
    _stem_along_route(
        rows, n_trips=1,
        value_key="min",
        ylabel="Delay attributed (min)",
        title=(f"Delay attribution — where delay accumulates "
               f"along Route 22 SB, trip {TARGET_TRIP_LABEL} "
               f"({TARGET_TRIP_ID})\n"
               f"Run: {trip_min:.1f} min  |  "
               f"OTHER (slowdown): {other_s / 60:.2f} min"),
        out=FIG_DIR / f"G3_stem_{TARGET_TRIP_LABEL}.png",
    )


# --------------------------------------------------------------------------
# H figures
# --------------------------------------------------------------------------


def slide_H_table(rows, other_secs, n_trips) -> None:
    print("[H] aggregate attribution table…")
    TOP_N = 50
    top = rows[:TOP_N]
    rest = rows[TOP_N:]
    if rest:
        rest_total = sum(r["total_min"] for r in rest)
        rest_mean = sum(r["mean_min"] for r in rest)
        rest_std = float(np.sqrt(sum(r["std_min"] ** 2 for r in rest)))
        top.append({
            "label": f"— All other {len(rest)} facilities (combined) —",
            "kind": "stop",
            "d_mi": float("nan"),
            "total_min": rest_total,
            "mean_min": rest_mean,
            "std_min": rest_std,
            "n": -1,
        })

    other_total_min = other_secs.sum() / 60
    other_mean_min = other_secs.mean() / 60
    other_std_min = other_secs.std() / 60

    _render_table(
        top,
        title=(
            f"Delay attribution — Aggregate across {n_trips} Route 22 SB trips"
        ),
        subtitle=(
            f"Top {TOP_N} facilities by mean delay.  "
            f"OTHER (slowdown, unattributed): total {other_total_min:.1f} min, "
            f"mean {other_mean_min:.2f} ± {other_std_min:.2f} min/trip"
        ),
        columns=["d_mi", "mean_min", "std_min", "total_min"],
        col_titles=["mile", "mean (min)", "std (min)", "total (min)"],
        out=FIG_DIR / "H1_waterfall_aggregate.png",
        n_cols=2,
    )


def slide_H_bar(rows, other_secs, n_trips) -> None:
    print("[H_bar] aggregate horizontal bar (mean ± std, top 25)…")
    TOP = 25
    top = rows[:TOP]
    fig, ax = plt.subplots(figsize=(14, 9), dpi=160)
    ax.set_facecolor("white")
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)

    labels = [_wrap_label(r["label"]) for r in top]
    means = np.array([r["mean_min"] for r in top])
    stds = np.array([r["std_min"] for r in top])
    colors = [_color(r["kind"]) for r in top]
    y = np.arange(len(top))[::-1]
    # Clip the lower error-bar whisker at 0 — delays cannot be negative, so
    # the left tail of ±σ has no physical meaning. Upper whisker unchanged.
    err_lo = np.minimum(stds, means)
    err_hi = stds
    ax.barh(y, means, xerr=[err_lo, err_hi], color=colors, edgecolor="#333",
            linewidth=0.4, height=0.78,
            error_kw=dict(ecolor="#333", lw=0.8, capsize=2.5))
    for yi, m, s in zip(y, means, stds):
        ax.text(m + s + 0.08, yi, f"{m:.2f} ± {s:.2f}", va="center",
                fontsize=8.5)
    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=9)
    # Lock the x-axis to the positive range. Right limit fits the bar+whisker
    # plus a little room for the right-aligned text label.
    x_max = float((means + stds).max())
    ax.set_xlim(0, x_max * 1.18)
    ax.set_xlabel("Mean delay attributed per trip (minutes, ± 1σ)",
                  fontsize=11)
    ax.set_title(
        f"Delay attribution — Top {TOP} facilities across {n_trips} "
        f"Route 22 SB trips\n"
        f"Mean ± std of attributed delay; OTHER bucket avg "
        f"{other_secs.mean() / 60:.2f} ± {other_secs.std() / 60:.2f} min/trip",
        fontsize=12, pad=10,
    )
    ax.grid(True, axis="x", alpha=0.3, linewidth=0.5)
    handles, lbls = _legend_handles()
    ax.legend(handles, lbls, loc="lower right", fontsize=9, frameon=True)
    fig.tight_layout()
    out = FIG_DIR / "H2_bar_aggregate.png"
    fig.savefig(out, dpi=160, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  saved: {out}")


def slide_H_bar_median(rows, other_secs, n_trips) -> None:
    print("[H_bar_median] aggregate horizontal bar (median + IQR, top 25)…")
    TOP = 25
    top = sorted(rows, key=lambda r: -r["median_min"])[:TOP]
    fig, ax = plt.subplots(figsize=(14, 9), dpi=160)
    ax.set_facecolor("white")
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)

    labels = [_wrap_label(r["label"]) for r in top]
    medians = np.array([r["median_min"] for r in top])
    q25 = np.array([r["q25_min"] for r in top])
    q75 = np.array([r["q75_min"] for r in top])
    err_lo = np.maximum(medians - q25, 0)
    err_hi = np.maximum(q75 - medians, 0)
    colors = [_color(r["kind"]) for r in top]
    y = np.arange(len(top))[::-1]
    ax.barh(y, medians, xerr=[err_lo, err_hi], color=colors,
            edgecolor="#333", linewidth=0.4, height=0.78,
            error_kw=dict(ecolor="#333", lw=0.8, capsize=2.5))
    for yi, m, lo, hi in zip(y, medians, q25, q75):
        ax.text(hi + 0.04, yi, f"{m:.2f}  [{lo:.2f}, {hi:.2f}]",
                va="center", fontsize=8.5)
    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=9)
    ax.set_xlabel("Median delay attributed per trip (minutes, whiskers = Q1–Q3)",
                  fontsize=11)
    other_med = np.percentile(other_secs, 50) / 60
    other_q25 = np.percentile(other_secs, 25) / 60
    other_q75 = np.percentile(other_secs, 75) / 60
    ax.set_title(
        f"Delay attribution — Top {TOP} facilities across {n_trips} "
        f"Route 22 SB trips\n"
        f"Median (whiskers Q1–Q3); OTHER bucket median "
        f"{other_med:.2f} min/trip [{other_q25:.2f}, {other_q75:.2f}]",
        fontsize=12, pad=10,
    )
    ax.grid(True, axis="x", alpha=0.3, linewidth=0.5)
    handles, lbls = _legend_handles()
    ax.legend(handles, lbls, loc="lower right", fontsize=9, frameon=True)
    fig.tight_layout()
    out = FIG_DIR / "H3_bar_aggregate_median.png"
    fig.savefig(out, dpi=160, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  saved: {out}")


def _stem_along_route(rows, n_trips, *, value_key: str, ylabel: str,
                      title: str, out: Path) -> None:
    fig, ax = plt.subplots(figsize=(16, 7), dpi=160)
    ax.set_facecolor("white")
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)

    for r in rows:
        c = _color(r["kind"])
        ax.vlines(r["d_mi"], 0, r[value_key], color=c, linewidth=1.6,
                  alpha=0.9)
        ax.scatter([r["d_mi"]], [r[value_key]], color=c, s=24,
                   edgecolor="black", linewidth=0.4, zorder=4)

    top = sorted(rows, key=lambda r: -r[value_key])[:8]
    for r in top:
        short = r["label"].split("@", 1)[-1].strip().split("/")[0].strip()
        if not short:
            short = r["label"]
        ax.annotate(_wrap_label(short, 22),
                    xy=(r["d_mi"], r[value_key]),
                    xytext=(0, 8), textcoords="offset points",
                    fontsize=8, ha="center", color="#333")

    ax.set_xlabel("Distance along Route 22 SB (mi)", fontsize=11)
    ax.set_ylabel(ylabel, fontsize=11)
    ax.set_title(title, fontsize=12, pad=10)
    ax.set_xlim(0, 11.0)
    ax.set_ylim(bottom=0)
    ax.grid(True, axis="y", alpha=0.3, linewidth=0.5)
    handles, lbls = _legend_handles()
    ax.legend(handles, lbls, loc="upper right", fontsize=9, frameon=True)
    fig.tight_layout()
    fig.savefig(out, dpi=160, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  saved: {out}")


def slide_H_stem(rows, n_trips) -> None:
    print("[H_stem] stem along route (mean)…")
    _stem_along_route(
        rows, n_trips,
        value_key="mean_min",
        ylabel="Mean delay attributed per trip (min)",
        title=(f"Delay attribution — where delay accumulates "
               f"along Route 22 SB, {n_trips} trips (mean per facility)"),
        out=FIG_DIR / "H4_stem.png",
    )


def slide_H_stem_median(rows, n_trips) -> None:
    print("[H_stem_median] stem along route (median)…")
    _stem_along_route(
        rows, n_trips,
        value_key="median_min",
        ylabel="Median delay attributed per trip (min)",
        title=(f"Delay attribution — where delay accumulates "
               f"along Route 22 SB, {n_trips} trips (median per facility)"),
        out=FIG_DIR / "H5_stem_median.png",
    )


def slide_H_buffer_stem(rows, n_trips) -> None:
    """Two-sided stem plot: mean delay up, buffer (p95 - mean) down.

    "Buffer time" is how much extra delay a rider should plan for at each
    facility relative to the typical (mean) trip — i.e. the tail thickness.
    Y-axis is symmetric in minutes so up/down stem heights are directly
    comparable.
    """
    print("[H_buffer_stem] mean + buffer (p95 - mean) stems along route…")
    fig, ax = plt.subplots(figsize=(16, 9), dpi=160)
    ax.set_facecolor("white")
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)

    # Compute buffer per row.
    for r in rows:
        r["_buffer_min"] = max(0.0, r["p95_min"] - r["mean_min"])

    for r in rows:
        c = _color(r["kind"])
        # Mean stem (up)
        ax.vlines(r["d_mi"], 0, r["mean_min"], color=c,
                  linewidth=1.6, alpha=0.9)
        ax.scatter([r["d_mi"]], [r["mean_min"]], color=c, s=24,
                   edgecolor="black", linewidth=0.4, zorder=4)
        # Buffer stem (down): thinner + lower alpha so the eye keeps the
        # mean as the primary value.
        if r["_buffer_min"] > 0:
            ax.vlines(r["d_mi"], 0, -r["_buffer_min"], color=c,
                      linewidth=1.4, alpha=0.55, linestyles="-")
            ax.scatter([r["d_mi"]], [-r["_buffer_min"]], color=c, s=18,
                       edgecolor="black", linewidth=0.3, alpha=0.7, zorder=4)

    # Annotate top facilities by mean (up) and by buffer (down).
    import re

    def _short_label(r):
        # For signals: "Traffic Signal @ X / Y" -> "X".
        # For stops/crossings: "<name> (<stop_id>)" -> "<name>".
        lab = r["label"]
        if "@" in lab:
            lab = lab.split("@", 1)[-1].strip().split("/")[0].strip()
        lab = re.sub(r"\s*\([^)]*\)\s*$", "", lab).strip()
        return _wrap_label(lab, 22) or r["label"]

    def _annotate_stagger(top, *, side: str, color: str):
        """Place annotations with x-staggered vertical offsets so labels
        from facilities within ~0.4 mi of each other don't overlap.

        ``side='up'`` -> labels above their stems; ``'down'`` -> below.
        """
        # Sort by x so we can sweep left-to-right and bump y-offset whenever
        # the next label is too close to the previous one(s).
        ordered = sorted(top, key=lambda r: r["d_mi"])
        sign = 1 if side == "up" else -1
        va = "bottom" if side == "up" else "top"
        base_offset = 8
        step_offset = 11
        min_dx_mi = 0.45  # facilities within this distance are likely to collide
        used: list[tuple[float, int]] = []  # (x, slot_index)
        for r in ordered:
            x = r["d_mi"]
            # Find a slot not in use by any label within min_dx_mi to the left.
            collisions = {slot for px, slot in used if abs(px - x) < min_dx_mi}
            slot = 0
            while slot in collisions:
                slot += 1
            used.append((x, slot))
            dy = sign * (base_offset + slot * step_offset)
            y_anchor = r["mean_min"] if side == "up" else -r["_buffer_min"]
            ax.annotate(_short_label(r),
                        xy=(x, y_anchor),
                        xytext=(0, dy), textcoords="offset points",
                        fontsize=8, ha="center", va=va, color=color)

    top_mean = sorted(rows, key=lambda r: -r["mean_min"])[:8]
    _annotate_stagger(top_mean, side="up", color="#333")
    top_buf = sorted(rows, key=lambda r: -r["_buffer_min"])[:6]
    _annotate_stagger(top_buf, side="down", color="#555")

    # Independent up/down limits so the top doesn't carry extra whitespace.
    # The up- and down-axes share the same minutes-per-unit scale by virtue
    # of being plotted on the same axes; only the *displayed* extents differ.
    y_top = max(r["mean_min"] for r in rows)
    y_bot = max(r["_buffer_min"] for r in rows)
    ax.set_ylim(-y_bot * 1.10, y_top * 1.20)  # 20% headroom on top for annotations
    ax.axhline(0, color="#333", linewidth=0.8, zorder=3)
    ax.set_xlim(0, 11.0)

    # Show the bottom-half tick labels as positive numbers (the negative
    # sign is purely a visual mirror, not a real negative value).
    from matplotlib.ticker import FuncFormatter
    ax.yaxis.set_major_formatter(FuncFormatter(lambda y, _: f"{abs(y):g}"))

    ax.set_xlabel("Distance along Route 22 SB (mi)", fontsize=11)
    # Place separate ylabels for each half so each sits in the correct
    # region (mean above the x-axis, buffer below).
    ax.set_ylabel("")
    ymin, ymax = ax.get_ylim()
    zero_frac = (0 - ymin) / (ymax - ymin)
    ax.text(-0.045, zero_frac + (1 - zero_frac) / 2,
            "Mean delay (min)",
            transform=ax.transAxes, rotation=90,
            ha="center", va="center", fontsize=11)
    ax.text(-0.045, zero_frac / 2,
            "Buffer (p95 − mean, min)",
            transform=ax.transAxes, rotation=90,
            ha="center", va="center", fontsize=11)
    ax.set_title(
        f"Delay attribution — mean + buffer time along Route 22 SB, "
        f"{n_trips} trips\n"
        f"Up: mean per-trip delay attributed to each facility.  "
        f"Down: 95th-percentile − mean (tail / buffer time).",
        fontsize=12, pad=10,
    )
    ax.grid(True, axis="y", alpha=0.3, linewidth=0.5)
    handles, lbls = _legend_handles()
    ax.legend(handles, lbls, loc="upper right", fontsize=9, frameon=True)
    fig.tight_layout()
    out = FIG_DIR / "H_buffer_stem.png"
    fig.savefig(out, dpi=160, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  saved: {out}")


# --- Stem variants: mean-only, +congestion, +congestion+buffer ------------


def _facility_short_label(r) -> str:
    import re
    lab = r["label"]
    if "@" in lab:
        lab = lab.split("@", 1)[-1].strip().split("/")[0].strip()
    lab = re.sub(r"\s*\([^)]*\)\s*$", "", lab).strip()
    return _wrap_label(lab, 22) or r["label"]


def _annotate_facility_stagger(ax, top, *, side: str, color: str, value_key: str):
    """Place facility labels staggered vertically to avoid x-collisions.

    ``value_key`` is the row key whose value sets the y-anchor (positive for
    'up', interpreted as -value for 'down').
    """
    ordered = sorted(top, key=lambda r: r["d_mi"])
    sign = 1 if side == "up" else -1
    va = "bottom" if side == "up" else "top"
    base_offset = 8
    step_offset = 11
    min_dx_mi = 0.45
    used: list[tuple[float, int]] = []
    for r in ordered:
        x = r["d_mi"]
        collisions = {slot for px, slot in used if abs(px - x) < min_dx_mi}
        slot = 0
        while slot in collisions:
            slot += 1
        used.append((x, slot))
        dy = sign * (base_offset + slot * step_offset)
        y_anchor = sign * r[value_key]
        ax.annotate(_facility_short_label(r),
                    xy=(x, y_anchor),
                    xytext=(0, dy), textcoords="offset points",
                    fontsize=8, ha="center", va=va, color=color)


def _stem_legend_handles(*, include_congestion: bool):
    handles = [plt.Rectangle((0, 0), 1, 1, color=KIND_COLOR[k])
               for k in KIND_ORDER]
    labels = [KIND_LABEL[k] for k in KIND_ORDER]
    if include_congestion:
        handles.append(plt.Rectangle((0, 0), 1, 1, color=CONG_COLOR))
        labels.append("congestion (per segment)")
    return handles, labels


def slide_H_stem_mean_only(rows, n_trips) -> None:
    """Mean-delay stems only — the upper half of H_buffer_stem."""
    print("[H_stem_mean_only] mean stems only…")
    fig, ax = plt.subplots(figsize=(16, 6), dpi=160)
    ax.set_facecolor("white")
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)

    for r in rows:
        c = _color(r["kind"])
        ax.vlines(r["d_mi"], 0, r["mean_min"], color=c, linewidth=1.6, alpha=0.9)
        ax.scatter([r["d_mi"]], [r["mean_min"]], color=c, s=24,
                   edgecolor="black", linewidth=0.4, zorder=4)

    top_mean = sorted(rows, key=lambda r: -r["mean_min"])[:8]
    _annotate_facility_stagger(ax, top_mean, side="up", color="#333",
                               value_key="mean_min")

    y_top = max(r["mean_min"] for r in rows)
    ax.set_ylim(0, y_top * 1.20)
    ax.set_xlim(0, 11.0)
    ax.set_xlabel("Distance along Route 22 SB (mi)", fontsize=11)
    ax.set_ylabel("Mean delay (min)", fontsize=11)
    ax.set_title(
        f"Delay attribution — mean per-trip delay along Route 22 SB, "
        f"{n_trips} trips",
        fontsize=12, pad=10,
    )
    ax.grid(True, axis="y", alpha=0.3, linewidth=0.5)
    handles, lbls = _stem_legend_handles(include_congestion=False)
    ax.legend(handles, lbls, loc="upper right", fontsize=9, frameon=True)
    fig.tight_layout()
    out = FIG_DIR / "H_stem_mean_only.png"
    fig.savefig(out, dpi=160, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  saved: {out}")


def slide_H_stem_with_congestion(rows, seg_rows, n_trips) -> None:
    """Mean stems + per-segment congestion stems (purple) at segment midpoints."""
    print("[H_stem_with_congestion] mean stems + congestion stems…")
    fig, ax = plt.subplots(figsize=(16, 6.5), dpi=160)
    ax.set_facecolor("white")
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)

    for r in seg_rows:
        if r["mean_min"] <= 0:
            continue
        ax.vlines(r["d_mi"], 0, r["mean_min"], color=CONG_COLOR,
                  linewidth=2.2, alpha=0.85, zorder=2)
        ax.scatter([r["d_mi"]], [r["mean_min"]], color=CONG_COLOR, s=30,
                   edgecolor="black", linewidth=0.4, zorder=3)

    for r in rows:
        c = _color(r["kind"])
        ax.vlines(r["d_mi"], 0, r["mean_min"], color=c, linewidth=1.6, alpha=0.9,
                  zorder=4)
        ax.scatter([r["d_mi"]], [r["mean_min"]], color=c, s=24,
                   edgecolor="black", linewidth=0.4, zorder=5)

    top_mean = sorted(rows, key=lambda r: -r["mean_min"])[:8]
    _annotate_facility_stagger(ax, top_mean, side="up", color="#333",
                               value_key="mean_min")

    y_top = max(
        max((r["mean_min"] for r in rows), default=0.0),
        max((r["mean_min"] for r in seg_rows), default=0.0),
    )
    ax.set_ylim(0, y_top * 1.20)
    ax.set_xlim(0, 11.0)
    ax.set_xlabel("Distance along Route 22 SB (mi)", fontsize=11)
    ax.set_ylabel("Mean delay (min)", fontsize=11)
    ax.set_title(
        f"Delay attribution — mean per-trip delay along Route 22 SB, "
        f"{n_trips} trips\n"
        f"Purple stems: residual congestion attributed to each "
        f"signal-to-signal segment (plotted at segment midpoint).",
        fontsize=12, pad=10,
    )
    ax.grid(True, axis="y", alpha=0.3, linewidth=0.5)
    handles, lbls = _stem_legend_handles(include_congestion=True)
    ax.legend(handles, lbls, loc="upper right", fontsize=9, frameon=True)
    fig.tight_layout()
    out = FIG_DIR / "H_stem_with_congestion.png"
    fig.savefig(out, dpi=160, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  saved: {out}")


def slide_H_buffer_stem_with_congestion(rows, seg_rows, n_trips) -> None:
    """Two-sided: mean (up) + buffer (down) for both facilities and segments."""
    print("[H_buffer_stem_with_congestion] mean + buffer stems "
          "incl. congestion…")
    fig, ax = plt.subplots(figsize=(16, 9), dpi=160)
    ax.set_facecolor("white")
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)

    for r in rows:
        r["_buffer_min"] = max(0.0, r["p95_min"] - r["mean_min"])
    for r in seg_rows:
        r["_buffer_min"] = max(0.0, r["p95_min"] - r["mean_min"])

    for r in seg_rows:
        if r["mean_min"] > 0:
            ax.vlines(r["d_mi"], 0, r["mean_min"], color=CONG_COLOR,
                      linewidth=2.2, alpha=0.85, zorder=2)
            ax.scatter([r["d_mi"]], [r["mean_min"]], color=CONG_COLOR, s=30,
                       edgecolor="black", linewidth=0.4, zorder=3)
        if r["_buffer_min"] > 0:
            ax.vlines(r["d_mi"], 0, -r["_buffer_min"], color=CONG_COLOR,
                      linewidth=1.8, alpha=0.5, zorder=2)
            ax.scatter([r["d_mi"]], [-r["_buffer_min"]], color=CONG_COLOR,
                       s=22, edgecolor="black", linewidth=0.3, alpha=0.7,
                       zorder=3)

    for r in rows:
        c = _color(r["kind"])
        ax.vlines(r["d_mi"], 0, r["mean_min"], color=c, linewidth=1.6,
                  alpha=0.9, zorder=4)
        ax.scatter([r["d_mi"]], [r["mean_min"]], color=c, s=24,
                   edgecolor="black", linewidth=0.4, zorder=5)
        if r["_buffer_min"] > 0:
            ax.vlines(r["d_mi"], 0, -r["_buffer_min"], color=c,
                      linewidth=1.4, alpha=0.55, zorder=4)
            ax.scatter([r["d_mi"]], [-r["_buffer_min"]], color=c, s=18,
                       edgecolor="black", linewidth=0.3, alpha=0.7, zorder=5)

    top_mean = sorted(rows, key=lambda r: -r["mean_min"])[:8]
    _annotate_facility_stagger(ax, top_mean, side="up", color="#333",
                               value_key="mean_min")
    top_buf = sorted(rows, key=lambda r: -r["_buffer_min"])[:6]
    _annotate_facility_stagger(ax, top_buf, side="down", color="#555",
                               value_key="_buffer_min")

    y_top = max(
        max((r["mean_min"] for r in rows), default=0.0),
        max((r["mean_min"] for r in seg_rows), default=0.0),
    )
    y_bot = max(
        max((r["_buffer_min"] for r in rows), default=0.0),
        max((r["_buffer_min"] for r in seg_rows), default=0.0),
    )
    ax.set_ylim(-y_bot * 1.10, y_top * 1.20)
    ax.axhline(0, color="#333", linewidth=0.8, zorder=3)
    ax.set_xlim(0, 11.0)

    from matplotlib.ticker import FuncFormatter
    ax.yaxis.set_major_formatter(FuncFormatter(lambda y, _: f"{abs(y):g}"))

    ax.set_xlabel("Distance along Route 22 SB (mi)", fontsize=11)
    ax.set_ylabel("")
    ymin, ymax = ax.get_ylim()
    zero_frac = (0 - ymin) / (ymax - ymin)
    ax.text(-0.045, zero_frac + (1 - zero_frac) / 2, "Mean delay (min)",
            transform=ax.transAxes, rotation=90,
            ha="center", va="center", fontsize=11)
    ax.text(-0.045, zero_frac / 2, "Buffer (p95 − mean, min)",
            transform=ax.transAxes, rotation=90,
            ha="center", va="center", fontsize=11)
    ax.set_title(
        f"Delay attribution — mean + buffer time along Route 22 SB, "
        f"{n_trips} trips\n"
        f"Up: mean per-trip delay.  Down: 95th-percentile − mean (buffer).  "
        f"Purple = residual congestion attributed per segment (at midpoint).",
        fontsize=12, pad=10,
    )
    ax.grid(True, axis="y", alpha=0.3, linewidth=0.5)
    handles, lbls = _stem_legend_handles(include_congestion=True)
    ax.legend(handles, lbls, loc="upper right", fontsize=9, frameon=True)
    fig.tight_layout()
    out = FIG_DIR / "H_buffer_stem_with_congestion.png"
    fig.savefig(out, dpi=160, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  saved: {out}")


def slide_H_map(rows, polyline, n_trips) -> None:
    print("[H_map] bubble map of facility delay…")
    import contextily as cx
    R = 6378137.0

    def to_merc(lats, lons):
        x = R * np.radians(np.asarray(lons, dtype=float))
        y = R * np.log(np.tan(np.pi / 4 + np.radians(np.asarray(lats, dtype=float)) / 2))
        return x, y

    sx, sy = to_merc(polyline[:, 0], polyline[:, 1])
    fig, ax = plt.subplots(figsize=(7, 14), dpi=160)
    ax.plot(sx, sy, color="#666", linewidth=2.5, alpha=0.7, zorder=2,
            solid_capstyle="round", label="Route 22 SB")

    pts = [(r["lat"], r["lon"], r["mean_min"], r["kind"], r["label"])
           for r in rows if r.get("lat") is not None and r.get("lon") is not None]
    if not pts:
        raise RuntimeError("no plottable facilities")
    means = np.array([p[2] for p in pts])
    max_mean = means.max()
    min_size, max_size = 18, 700
    sizes = min_size + (means / max_mean) * (max_size - min_size)

    for (lat, lon, mn, kind, lab), sz in zip(pts, sizes):
        x, y = to_merc(np.array([lat]), np.array([lon]))
        ax.scatter(x, y, s=sz, color=_color(kind),
                   edgecolor="black", linewidth=0.6, alpha=0.78, zorder=4)

    top = sorted(pts, key=lambda p: -p[2])[:6]
    for lat, lon, mn, kind, lab in top:
        x, y = to_merc(np.array([lat]), np.array([lon]))
        short = lab.split("@", 1)[-1].strip().split("/")[0].strip() or lab
        ax.annotate(f"{_wrap_label(short, 22)}\n{mn:.2f} min",
                    xy=(x[0], y[0]), xytext=(8, 0),
                    textcoords="offset points", fontsize=7.5,
                    ha="left", va="center", color="#222",
                    bbox=dict(facecolor="white", edgecolor="#888",
                              boxstyle="round,pad=0.2", linewidth=0.5),
                    zorder=6)

    pad = 600
    ax.set_xlim(sx.min() - pad, sx.max() + pad)
    ax.set_ylim(sy.min() - pad, sy.max() + pad)
    cx.add_basemap(ax, source=cx.providers.CartoDB.PositronNoLabels,
                   crs="EPSG:3857", attribution_size=6)
    ax.set_xticks([])
    ax.set_yticks([])

    handles, lbls = _legend_handles()
    leg = ax.legend(handles, lbls, loc="upper right", fontsize=9,
                    frameon=True)
    ax.add_artist(leg)
    ax.set_title(
        f"Delay attribution — {n_trips} Route 22 SB trips\n"
        f"bubble size ∝ mean delay/trip; max = {max_mean:.2f} min",
        fontsize=11, pad=8,
    )
    fig.tight_layout()
    out = FIG_DIR / "H6_map_bubbles.png"
    fig.savefig(out, dpi=160, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  saved: {out}")


def slide_H_3d_map_stem(rows, polyline, n_trips) -> None:
    print("[H_3d_map] perspective stem plot over basemap…")
    import contextily as cx
    from mpl_toolkits.mplot3d import Axes3D  # noqa: F401

    R = 6378137.0

    def merc(lat, lon):
        lat = np.asarray(lat, dtype=float)
        lon = np.asarray(lon, dtype=float)
        x = R * np.radians(lon)
        y = R * np.log(np.tan(np.pi / 4 + np.radians(lat) / 2))
        return x, y

    sx, sy = merc(polyline[:, 0], polyline[:, 1])
    pad = 0.04
    x_min, x_max = float(sx.min()), float(sx.max())
    y_min, y_max = float(sy.min()), float(sy.max())
    px = (x_max - x_min) * pad
    py = (y_max - y_min) * pad
    x_min -= px
    x_max += px
    y_min -= py
    y_max += py

    img, ext = cx.bounds2img(x_min, y_min, x_max, y_max,
                             source=cx.providers.CartoDB.PositronNoLabels,
                             ll=False)
    if img.shape[2] == 4:
        img = img[:, :, :3]
    img = img.astype(np.float32) / 255.0

    ext_xmin, ext_xmax, ext_ymin, ext_ymax = ext
    H0, W0 = img.shape[:2]
    col0 = max(0, int(round((x_min - ext_xmin) / (ext_xmax - ext_xmin) * W0)))
    col1 = min(W0, int(round((x_max - ext_xmin) / (ext_xmax - ext_xmin) * W0)))
    row0 = max(0, int(round((ext_ymax - y_max) / (ext_ymax - ext_ymin) * H0)))
    row1 = min(H0, int(round((ext_ymax - y_min) / (ext_ymax - ext_ymin) * H0)))
    img = img[row0:row1, col0:col1]

    H, W = img.shape[:2]
    MAX_DIM = 280
    if max(H, W) > MAX_DIM:
        step_h = max(1, H // MAX_DIM)
        step_w = max(1, W // MAX_DIM)
        img = img[::step_h, ::step_w]
        H, W = img.shape[:2]

    xs_grid = np.linspace(x_min, x_max, W + 1)
    ys_grid = np.linspace(y_max, y_min, H + 1)
    Xg, Yg = np.meshgrid(xs_grid, ys_grid)
    max_mean_pre = max(r["mean_min"] for r in rows)
    surface_z = -0.02 * max_mean_pre
    Zg = np.full_like(Xg, surface_z)

    fig = plt.figure(figsize=(7.5, 13), dpi=160)
    ax = fig.add_subplot(111, projection="3d")
    fig.subplots_adjust(left=-0.05, right=1.05, top=0.97, bottom=-0.05)

    ax.plot_surface(Xg, Yg, Zg, facecolors=img, rstride=1, cstride=1,
                    shade=False, antialiased=False, edgecolor="none")
    ax.plot(sx, sy, np.zeros_like(sx), color="#222", linewidth=2.0,
            alpha=0.9, zorder=4)

    pts = [(r["lat"], r["lon"], r["mean_min"], r["kind"], r["label"])
           for r in rows if r.get("lat") is not None and r.get("lon") is not None]
    fxs = np.array([float(merc(p[0], p[1])[0]) for p in pts])
    fys = np.array([float(merc(p[0], p[1])[1]) for p in pts])
    heights = np.array([p[2] for p in pts])
    bar_colors = [_color(p[3]) for p in pts]
    bar_w = (x_max - x_min) * 0.012
    ax.bar3d(fxs - bar_w / 2, fys - bar_w / 2, np.zeros_like(fxs),
             bar_w, bar_w, heights,
             color=bar_colors, edgecolor="black", linewidth=0.25,
             shade=True, alpha=0.95)

    top = sorted(rows, key=lambda r: -r["mean_min"])[:6]
    for r in top:
        if r.get("lat") is None:
            continue
        fx, fy = merc(r["lat"], r["lon"])
        short = r["label"].split("@", 1)[-1].strip().split("/")[0].strip()
        if not short:
            short = r["label"]
        ax.text(float(fx) + (x_max - x_min) * 0.005,
                float(fy), r["mean_min"] * 1.05, short,
                fontsize=8.5, color="#111", ha="left")

    ax.set_xlim(x_min, x_max)
    ax.set_ylim(y_min, y_max)
    max_mean = max(r["mean_min"] for r in rows)
    ax.set_zlim(0, max_mean * 1.15)
    dx = x_max - x_min
    dy = y_max - y_min
    ax.set_box_aspect((dx * 1.8, dy, max(dx, dy) * 0.55))
    ax.view_init(elev=28, azim=-70)

    ax.set_xticks([])
    ax.set_yticks([])
    ax.xaxis.pane.set_visible(False)
    ax.yaxis.pane.set_visible(False)
    ax.zaxis.pane.set_visible(False)
    ax.xaxis.line.set_color((1, 1, 1, 0))
    ax.yaxis.line.set_color((1, 1, 1, 0))
    ax.set_zlabel("Mean delay attributed (min/trip)", fontsize=10)
    ax.grid(False)

    ax.set_title(
        f"Delay attribution — mean per facility ({n_trips} trips)",
        fontsize=12, pad=4,
    )
    handles, lbls = _legend_handles()
    ax.legend(handles, lbls, loc="upper right", fontsize=9, frameon=True)

    out = FIG_DIR / "H7_3d_map_stem.png"
    fig.savefig(out, dpi=160, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  saved: {out}")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def main() -> int:
    FIG_DIR.mkdir(parents=True, exist_ok=True)
    segments, ff, facility_index, polyline, _dist_along = load_corridor_objects()
    print(f"Loaded {len(segments)} segments, {len(facility_index)} facilities, "
          f"free-flow table covers {len(ff)} segments")

    # G — per-trip
    g_rows, g_other_s, g_t_obs_s = compute_g_data(segments, ff, facility_index)
    slide_G_table(g_rows, g_other_s, g_t_obs_s)
    slide_G_bar(g_rows, g_other_s, g_t_obs_s)
    slide_G_stem(g_rows, g_other_s, g_t_obs_s)

    # H — aggregate
    h_rows, h_other_secs, n_trips, seg_rows = compute_h_data(
        segments, ff, facility_index)
    slide_H_table(h_rows, h_other_secs, n_trips)
    slide_H_bar(h_rows, h_other_secs, n_trips)
    slide_H_bar_median(h_rows, h_other_secs, n_trips)
    slide_H_stem(h_rows, n_trips)
    slide_H_stem_median(h_rows, n_trips)
    slide_H_buffer_stem(h_rows, n_trips)
    slide_H_stem_mean_only(h_rows, n_trips)
    slide_H_stem_with_congestion(h_rows, seg_rows, n_trips)
    slide_H_buffer_stem_with_congestion(h_rows, seg_rows, n_trips)
    slide_H_map(h_rows, polyline, n_trips)
    slide_H_3d_map_stem(h_rows, polyline, n_trips)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
