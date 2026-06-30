"""Build slides G + H — delay attribution for trip 1001350 and aggregated
across all 431 archive SB trips on pattern 3936.

  slides/G_delay_attribution_1001350.png
  slides/H_delay_attribution_aggregate.png

Algorithm (per trip):
  1. Identify slow windows (v < 5 mph, ≥ 2 s).
  2. For each window, look for bus stops with dist_along ∈ [x_a, x_b].
     If any → split window's duration evenly across them.
  3. Else, look for intersections (signal or stop sign) with dist_along
     ∈ [x_a, x_b + 0.05 mi]. If any → split evenly.
  4. Else → OTHER bucket.

Reuses the same trip pool that produced F4 (r2_route22_sb_all.csv).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.interpolate import CubicHermiteSpline

PYTHONPATH_SRC = str((Path(__file__).resolve().parent.parent / "src"))
if PYTHONPATH_SRC not in sys.path:
    sys.path.insert(0, PYTHONPATH_SRC)

from bus_trajectories.intersections import load_intersections  # noqa: E402
from bus_trajectories.io import load_route_stops  # noqa: E402
from bus_trajectories.pipeline import reconstruct_csv  # noqa: E402

TRIP_ID = "1001350"
SHAPE_ID = "67803936"
PATTERN = "3936"
ROUTE = "22"
GTFS = "cta_gtfs.zip"
M_PER_MI = 1609.344
THRESH_MPS = 5.0 / 2.23694
LOOKAHEAD_M = 0.05 * M_PER_MI
SLIDES = Path("figures")
ALL_CSV = Path("r2_route22_sb_all.csv")


EXCLUDED_INTERSECTION_SUBSTRINGS = (
    "Paulina",            # route-start signal at Howard terminal
    "Harrison Street Bikeway",  # route-end signal near Harrison
)


def build_features() -> tuple[list[dict], list[dict]]:
    stops = []
    for s in load_route_stops(GTFS, SHAPE_ID):
        stops.append({"label": s["name"], "kind": "stop", "d_m": s["dist_along_m"]})
    inters = []
    for cp in load_intersections("intersections_route22.json")[SHAPE_ID]:
        cross = " / ".join(cp.cross_street_names) if cp.cross_street_names else "(unnamed)"
        if any(sub in cross for sub in EXCLUDED_INTERSECTION_SUBSTRINGS):
            continue
        mi = cp.dist_along_route_m / M_PER_MI
        if cp.control_type == "traffic_signals":
            inters.append({"label": f"Traffic Signal @ Clark & {cross}",
                           "kind": "traffic_signals", "d_m": cp.dist_along_route_m})
        elif cp.control_type == "stop":
            inters.append({"label": f"Stop Sign @ Clark & {cross}",
                           "kind": "stop_sign", "d_m": cp.dist_along_route_m})
        elif cp.control_type == "ped_crossing_signal":
            inters.append({"label": f"Ped Signal @ Clark mi {mi:.2f}",
                           "kind": "ped_signal", "d_m": cp.dist_along_route_m})
        elif cp.control_type == "ped_crossing_marked":
            inters.append({"label": f"Crosswalk @ Clark mi {mi:.2f}",
                           "kind": "ped_marked", "d_m": cp.dist_along_route_m})
    stops.sort(key=lambda ft: ft["d_m"])
    inters.sort(key=lambda ft: ft["d_m"])
    for i, ft in enumerate(stops):  ft["id"] = ("stop", i)
    for i, ft in enumerate(inters): ft["id"] = ("int",  i)
    return stops, inters


def attribute_trip(t_knots, x_knots, slopes,
                    stops, inters) -> tuple[dict, float, int]:
    """Return (per-feature seconds dict, OTHER seconds, n_other_windows)."""
    f = CubicHermiteSpline(t_knots, x_knots, slopes, extrapolate=False)
    ts = np.linspace(t_knots[0], t_knots[-1], 20000)
    xs = f(ts); vs = f.derivative()(ts)

    slow = vs < THRESH_MPS
    windows, in_run, i_start = [], False, 0
    for i, s in enumerate(slow):
        if s and not in_run: i_start = i; in_run = True
        elif not s and in_run: windows.append((i_start, i - 1)); in_run = False
    if in_run: windows.append((i_start, len(slow) - 1))
    windows = [(a, b) for a, b in windows if (ts[b] - ts[a]) >= 2.0]

    stop_d = np.array([ft["d_m"] for ft in stops])
    int_d  = np.array([ft["d_m"] for ft in inters])
    sec_per: dict = {}
    other_total = 0.0
    other_w = 0

    for a, b in windows:
        dur = ts[b] - ts[a]
        x_a, x_b = xs[a], xs[b]
        s_idx = np.where((stop_d >= x_a) & (stop_d <= x_b))[0]
        if len(s_idx):
            share = dur / len(s_idx)
            for j in s_idx:
                sec_per[stops[j]["id"]] = sec_per.get(stops[j]["id"], 0.0) + share
            continue
        i_idx = np.where((int_d >= x_a) & (int_d <= x_b + LOOKAHEAD_M))[0]
        if len(i_idx):
            share = dur / len(i_idx)
            for j in i_idx:
                sec_per[inters[j]["id"]] = sec_per.get(inters[j]["id"], 0.0) + share
            continue
        other_total += dur; other_w += 1
    return sec_per, other_total, other_w


# ---------------------------- G: single trip ------------------------------


def slide_G() -> None:
    print("[G] single-trip delay attribution table…")
    stops, inters = build_features()
    feature_index = {ft["id"]: ft for ft in stops + inters}

    rec = next(t for t in json.load(open("out_r2_bw5/trajectories.json"))["trips"]
               if t["trip_id"] == TRIP_ID)
    t = np.asarray(rec["t_knots"]); x = np.asarray(rec["x_knots"]); m = np.asarray(rec["slopes"])
    sec_per, other_s, _ = attribute_trip(t, x, m, stops, inters)

    rows = []
    for fid, sec in sec_per.items():
        if sec <= 0: continue
        ft = feature_index[fid]
        rows.append({"label": ft["label"], "kind": ft["kind"],
                     "d_mi": ft["d_m"]/M_PER_MI, "min": sec/60})
    rows.sort(key=lambda r: -r["min"])
    total_s = sum(r["min"] for r in rows) * 60
    trip_min = (t[-1]-t[0])/60

    _render_table(
        rows, other_s,
        title=f"Naive Delay — Trip {TRIP_ID} attribution (v < 5 mph)",
        subtitle=(f"Run: {trip_min:.1f} min  |  Delay: {(total_s+other_s)/60:.1f} min "
                  f"({(total_s+other_s)/(t[-1]-t[0])*100:.0f}%)  |  "
                  f"Attributed: {total_s/60:.1f} min ({total_s/(total_s+other_s)*100:.0f}%)  |  "
                  f"OTHER: {other_s/60:.2f} min"),
        columns=["d_mi", "min"],
        col_titles=["mile", "delay (min)"],
        out=SLIDES / "G_delay_attribution_1001350.png",
        n_cols=2,
    )


# ---------------------------- H: aggregated -------------------------------


def slide_H() -> None:
    print("[H] aggregate delay attribution across all 431 archive trips…")
    stops, inters = build_features()
    feature_index = {ft["id"]: ft for ft in stops + inters}

    print("  reconstructing all trips (this is slow — ~431 smoothings)…")
    recons = reconstruct_csv(
        csv_path=ALL_CSV,
        gtfs_zip_path=GTFS,
        route_id=ROUTE,
        pattern_id=PATTERN,
        bandwidth=5,
    )
    print(f"  reconstructed {len(recons)} trips")

    # Per-feature list of seconds (one entry per trip; missing = 0)
    per_feature: dict = {fid: np.zeros(len(recons)) for fid in feature_index}
    other_secs = np.zeros(len(recons))
    for i, (tid, r) in enumerate(recons.items()):
        # Pull (t, x, m) from smoothed PCHIP
        f = r.smoothed.f
        t = f.x
        x = f(t)
        m = f.derivative()(t)
        sec_per, other_s, _ = attribute_trip(t, x, m, stops, inters)
        for fid, sec in sec_per.items():
            per_feature[fid][i] = sec
        other_secs[i] = other_s

    rows = []
    for fid, ft in feature_index.items():
        arr = per_feature[fid]
        n_with = int((arr > 0).sum())
        if n_with == 0: continue
        rows.append({
            "label": ft["label"], "kind": ft["kind"],
            "d_mi": ft["d_m"]/M_PER_MI,
            "total_min": arr.sum()/60,
            "mean_min": arr.mean()/60,
            "std_min": arr.std()/60,
            "n": n_with,
        })
    # Sort by mean delay desc
    rows.sort(key=lambda r: -r["mean_min"])

    # Show top N; collapse the rest into one "All other features" row.
    TOP_N = 50
    top = rows[:TOP_N]
    rest = rows[TOP_N:]
    if rest:
        rest_arr_total = sum(r["total_min"] for r in rest)
        rest_arr_mean = sum(r["mean_min"] for r in rest)
        # std of a sum of independent vars ≈ sqrt(sum var); but features overlap,
        # so just report the rms as a rough scale indicator.
        rest_arr_std = float(np.sqrt(sum(r["std_min"] ** 2 for r in rest)))
        top.append({
            "label": f"— All other {len(rest)} features (combined) —",
            "kind": "stop",  # neutral
            "d_mi": float("nan"),
            "total_min": rest_arr_total,
            "mean_min": rest_arr_mean,
            "std_min": rest_arr_std,
            "n": -1,
        })

    other_total_min = other_secs.sum() / 60
    other_mean_min = other_secs.mean() / 60
    other_std_min = other_secs.std() / 60

    n_trips = len(recons)
    _render_table(
        top, None,
        title=(f"Naive Delay — Aggregate attribution, all {n_trips} archive Route 22 SB trips"),
        subtitle=(f"v < 5 mph; stops first, then intersections within "
                   f"window or 0.05 mi ahead.  Top {TOP_N} features by mean delay.  "
                   f"OTHER: total {other_total_min:.1f} min, "
                   f"mean {other_mean_min:.2f} ± {other_std_min:.2f} min/trip"),
        columns=["d_mi", "mean_min", "std_min", "total_min"],
        col_titles=["mile", "mean (min)", "std (min)", "total (min)"],
        out=SLIDES / "H_delay_attribution_aggregate.png",
        n_cols=2,
        aggregate=True,
    )


# ---------------------------- shared renderer -----------------------------


def _kind_color(kind: str) -> str:
    return {
        "stop": "#3a85d6",
        "traffic_signals": "#dc8c32",
        "stop_sign": "#cc0000",
    }.get(kind, "#888")


def _kind_short(kind: str) -> str:
    return {"stop": "stop", "traffic_signals": "signal",
            "stop_sign": "stop sign"}.get(kind, kind)


def _render_table(rows, other_s, *, title, subtitle, columns, col_titles,
                   out: Path, n_cols: int = 2, aggregate: bool = False) -> None:
    n_rows = len(rows)
    per_col = (n_rows + n_cols - 1) // n_cols
    fig_h = max(9.0, 0.26 * per_col + 2.2)
    fig_w = 16.0

    fig, axes = plt.subplots(1, n_cols, figsize=(fig_w, fig_h), dpi=160)
    if n_cols == 1:
        axes = [axes]

    title_y = 1 - 0.6 / fig_h
    sub_y   = 1 - 1.05 / fig_h
    fig.suptitle(title, fontsize=16, fontweight="bold", y=title_y)
    fig.text(0.5, sub_y, subtitle, ha="center", fontsize=10, color="#444")

    for ci, ax in enumerate(axes):
        ax.set_xticks([]); ax.set_yticks([])
        for s in ("top", "right", "left", "bottom"):
            ax.spines[s].set_visible(False)
        chunk = rows[ci * per_col : (ci + 1) * per_col]
        if not chunk: continue

        # Column header
        header_y = 1.0
        ax.text(0.02, header_y, "feature", fontsize=9, fontweight="bold",
                transform=ax.transAxes, va="bottom")
        # Lay numeric columns out evenly from right edge inward.
        n_num = len(columns)
        right_edge = 0.99
        col_step = 0.085
        x_offsets = {
            col: right_edge - (n_num - 1 - i) * col_step
            for i, col in enumerate(columns)
        }
        col_align = {col: "right" for col in columns}
        for col, title_text in zip(columns, col_titles):
            ax.text(x_offsets[col], header_y, title_text, fontsize=9,
                    fontweight="bold", transform=ax.transAxes,
                    ha=col_align[col], va="bottom")
        ax.plot([0, 1], [header_y - 0.005, header_y - 0.005],
                 color="#888", linewidth=0.6, transform=ax.transAxes,
                 clip_on=False)

        for i, row in enumerate(chunk):
            y = header_y - 0.020 - (i + 0.5) * (1.0 / max(per_col, 1))
            color = _kind_color(row["kind"])
            # Color swatch
            ax.add_patch(plt.Rectangle((0.005, y - 0.011), 0.012, 0.022,
                                         transform=ax.transAxes,
                                         facecolor=color, edgecolor="none",
                                         clip_on=False))
            # Truncate to leave room for the leftmost numeric column.
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

    # Legend with colored swatches
    legend_items = [("stop", "#3a85d6"), ("signal", "#dc8c32"), ("stop sign", "#cc0000")]
    x = 0.42
    for name, col in legend_items:
        fig.text(x, 0.012, "■", color=col, fontsize=12, ha="left", va="bottom")
        fig.text(x + 0.012, 0.012, name, color="#444", fontsize=9, ha="left", va="bottom")
        x += 0.07

    top_frac = 1 - 1.6 / fig_h
    fig.subplots_adjust(left=0.03, right=0.99, top=top_frac, bottom=0.05, wspace=0.10)
    fig.savefig(out, dpi=160, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  saved: {out}")


# ---------------------------- Bar / stem / map ----------------------------


def _kind_color_full(kind: str) -> str:
    return {
        "stop": "#3a85d6",            # bus stop — blue
        "traffic_signals": "#dc8c32", # traffic signal — amber
        "stop_sign": "#cc0000",       # stop sign — red
        "ped_signal": "#7b3fa0",      # signalised crosswalk — purple
        "ped_marked": "#00897b",      # marked crosswalk — teal
    }.get(kind, "#888")


def _wrap_label(label: str, max_chars: int = 38) -> str:
    if len(label) <= max_chars:
        return label
    return label[: max_chars - 1] + "…"


def _compute_g_data():
    stops, inters = build_features()
    feature_index = {ft["id"]: ft for ft in stops + inters}
    rec = next(t for t in json.load(open("out_r2_bw5/trajectories.json"))["trips"]
               if t["trip_id"] == TRIP_ID)
    t = np.asarray(rec["t_knots"]); x = np.asarray(rec["x_knots"]); m = np.asarray(rec["slopes"])
    sec_per, other_s, _ = attribute_trip(t, x, m, stops, inters)
    rows = []
    for fid, sec in sec_per.items():
        if sec <= 0: continue
        ft = feature_index[fid]
        rows.append({"label": ft["label"], "kind": ft["kind"],
                     "d_mi": ft["d_m"]/M_PER_MI,
                     "lat": ft.get("lat"), "lon": ft.get("lon"),
                     "min": sec/60})
    rows.sort(key=lambda r: -r["min"])
    return rows, other_s, t[-1]-t[0]


def slide_G_bar() -> None:
    print("[G_bar] horizontal bar chart for trip 1001350…")
    rows, other_s, dur_s = _compute_g_data()
    TOP = 25
    rows = rows[:TOP]

    fig, ax = plt.subplots(figsize=(14, 9), dpi=160)
    ax.set_facecolor("white")
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)

    labels = [_wrap_label(r["label"]) for r in rows]
    values = [r["min"] for r in rows]
    colors = [_kind_color_full(r["kind"]) for r in rows]
    y = np.arange(len(rows))[::-1]
    ax.barh(y, values, color=colors, edgecolor="#333", linewidth=0.4, height=0.78)
    for yi, v in zip(y, values):
        ax.text(v + 0.04, yi, f"{v:.2f}", va="center", fontsize=8.5)
    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=9)
    ax.set_xlabel("Delay attributed (minutes)", fontsize=11)
    trip_min = dur_s / 60
    total_attr_min = sum(values)
    ax.set_title(
        f"Naive Delay — Trip {TRIP_ID}, top {len(rows)} sources\n"
        f"Run: {trip_min:.1f} min  |  Attributed delay (top {TOP}): "
        f"{total_attr_min:.1f} min  |  OTHER: {other_s/60:.2f} min",
        fontsize=12, pad=10,
    )
    ax.grid(True, axis="x", alpha=0.3, linewidth=0.5)

    # Legend
    handles = [plt.Rectangle((0, 0), 1, 1, color=_kind_color_full(k))
               for k in ("stop", "traffic_signals", "stop_sign",
                          "ped_signal", "ped_marked")]
    ax.legend(handles, ["bus stop", "traffic signal", "stop sign",
                          "ped signal", "crosswalk"],
              loc="lower right", fontsize=9, frameon=True)
    fig.tight_layout()
    out = SLIDES / "G_bar_1001350.png"
    fig.savefig(out, dpi=160, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  saved: {out}")


def _compute_h_data():
    """Return (rows_list, other_secs_arr, n_trips, polyline, dist_along)."""
    from bus_trajectories.io import load_gtfs_shape_with_dist
    stops, inters = build_features()
    feature_index = {ft["id"]: ft for ft in stops + inters}
    poly, dist_along = load_gtfs_shape_with_dist(GTFS, SHAPE_ID)

    # Project stops back to lat/lon via polyline
    for ft in stops:
        idx = int(np.argmin(np.abs(dist_along - ft["d_m"])))
        ft["lat"] = float(poly[idx, 0])
        ft["lon"] = float(poly[idx, 1])
    # Intersections already have lat/lon — pull from ControlPoint list
    cp_lookup = {}
    for cp in load_intersections("intersections_route22.json")[SHAPE_ID]:
        cross = " / ".join(cp.cross_street_names) if cp.cross_street_names else "(unnamed)"
        if cp.control_type == "traffic_signals":
            label = f"Traffic Signal @ Clark & {cross}"
        elif cp.control_type == "stop":
            label = f"Stop Sign @ Clark & {cross}"
        else:
            continue
        cp_lookup[label] = (cp.lat, cp.lon)
    for ft in inters:
        latlon = cp_lookup.get(ft["label"])
        if latlon:
            ft["lat"], ft["lon"] = latlon

    print("  reconstructing all trips (this is slow)…")
    recons = reconstruct_csv(csv_path=ALL_CSV, gtfs_zip_path=GTFS,
                              route_id=ROUTE, pattern_id=PATTERN, bandwidth=5)
    print(f"  reconstructed {len(recons)} trips")

    per_feature = {fid: np.zeros(len(recons)) for fid in feature_index}
    other_secs = np.zeros(len(recons))
    for i, (tid, r) in enumerate(recons.items()):
        f = r.smoothed.f
        t_knots = f.x; x_knots = f(t_knots); m_knots = f.derivative()(t_knots)
        sec_per, other_s, _ = attribute_trip(t_knots, x_knots, m_knots, stops, inters)
        for fid, sec in sec_per.items():
            per_feature[fid][i] = sec
        other_secs[i] = other_s

    rows = []
    for fid, ft in feature_index.items():
        arr = per_feature[fid]
        n_with = int((arr > 0).sum())
        if n_with == 0: continue
        q25, med, q75 = np.percentile(arr, [25, 50, 75]) / 60
        rows.append({
            "label": ft["label"], "kind": ft["kind"],
            "d_mi": ft["d_m"] / M_PER_MI,
            "lat": ft.get("lat"), "lon": ft.get("lon"),
            "total_min": arr.sum()/60,
            "mean_min": arr.mean()/60,
            "std_min": arr.std()/60,
            "median_min": float(med),
            "q25_min": float(q25),
            "q75_min": float(q75),
            "n": n_with,
        })
    rows.sort(key=lambda r: -r["mean_min"])
    return rows, other_secs, len(recons), poly, dist_along


def slide_H_bar(rows, other_secs, n_trips) -> None:
    print("[H_bar] aggregate horizontal bar chart…")
    TOP = 25
    top = rows[:TOP]
    fig, ax = plt.subplots(figsize=(14, 9), dpi=160)
    ax.set_facecolor("white")
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)

    labels = [_wrap_label(r["label"]) for r in top]
    means = np.array([r["mean_min"] for r in top])
    stds = np.array([r["std_min"] for r in top])
    colors = [_kind_color_full(r["kind"]) for r in top]
    y = np.arange(len(top))[::-1]
    ax.barh(y, means, xerr=stds, color=colors, edgecolor="#333",
            linewidth=0.4, height=0.78,
            error_kw=dict(ecolor="#333", lw=0.8, capsize=2.5))
    for yi, m, s in zip(y, means, stds):
        ax.text(m + s + 0.08, yi, f"{m:.2f} ± {s:.2f}",
                va="center", fontsize=8.5)
    ax.set_yticks(y); ax.set_yticklabels(labels, fontsize=9)
    ax.set_xlabel("Mean delay attributed per trip (minutes, ± 1σ)", fontsize=11)
    ax.set_title(
        f"Naive Delay — Top {TOP} sources across {n_trips} archive Route 22 SB trips\n"
        f"Mean ± std of attributed delay; OTHER bucket avg "
        f"{other_secs.mean()/60:.2f} ± {other_secs.std()/60:.2f} min/trip",
        fontsize=12, pad=10,
    )
    ax.grid(True, axis="x", alpha=0.3, linewidth=0.5)
    handles = [plt.Rectangle((0, 0), 1, 1, color=_kind_color_full(k))
               for k in ("stop", "traffic_signals", "stop_sign",
                          "ped_signal", "ped_marked")]
    ax.legend(handles, ["bus stop", "traffic signal", "stop sign",
                          "ped signal", "crosswalk"],
              loc="lower right", fontsize=9, frameon=True)
    fig.tight_layout()
    out = SLIDES / "H_bar_aggregate.png"
    fig.savefig(out, dpi=160, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  saved: {out}")


def slide_H_stem(rows, n_trips) -> None:
    print("[H_stem] aggregate stem plot along route…")
    fig, ax = plt.subplots(figsize=(16, 7), dpi=160)
    ax.set_facecolor("white")
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)

    for r in rows:
        c = _kind_color_full(r["kind"])
        ax.vlines(r["d_mi"], 0, r["mean_min"], color=c, linewidth=1.6, alpha=0.9)
        ax.scatter([r["d_mi"]], [r["mean_min"]], color=c, s=24,
                    edgecolor="black", linewidth=0.4, zorder=4)

    # Annotate top features
    rows_sorted_by_mean = sorted(rows, key=lambda r: -r["mean_min"])[:8]
    for r in rows_sorted_by_mean:
        short = r["label"].split("@", 1)[-1].strip().replace("Clark & ", "").split("/")[0].strip()
        if not short: short = r["label"]
        ax.annotate(_wrap_label(short, 22),
                     xy=(r["d_mi"], r["mean_min"]),
                     xytext=(0, 8), textcoords="offset points",
                     fontsize=8, ha="center", color="#333")

    ax.set_xlabel("Distance along Route 22 SB (mi)", fontsize=11)
    ax.set_ylabel("Mean delay attributed per trip (min)", fontsize=11)
    ax.set_title(
        f"Naive Delay — Where delay accumulates along Route 22 SB, {n_trips} trips, mean per feature",
        fontsize=12, pad=10,
    )
    ax.set_xlim(0, 11.0)
    ax.set_ylim(bottom=0)
    ax.grid(True, axis="y", alpha=0.3, linewidth=0.5)
    handles = [plt.Rectangle((0, 0), 1, 1, color=_kind_color_full(k))
               for k in ("stop", "traffic_signals", "stop_sign",
                          "ped_signal", "ped_marked")]
    ax.legend(handles, ["bus stop", "traffic signal", "stop sign",
                          "ped signal", "crosswalk"],
              loc="upper right", fontsize=9, frameon=True)
    fig.tight_layout()
    out = SLIDES / "H_stem_along_route.png"
    fig.savefig(out, dpi=160, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  saved: {out}")


def slide_H_bar_median(rows, other_secs, n_trips) -> None:
    """Top-25 horizontal bars showing median with Q1-Q3 IQR whiskers."""
    print("[H_bar_median] aggregate horizontal bar chart (median + IQR)…")
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
    colors = [_kind_color_full(r["kind"]) for r in top]
    y = np.arange(len(top))[::-1]
    ax.barh(y, medians, xerr=[err_lo, err_hi], color=colors, edgecolor="#333",
            linewidth=0.4, height=0.78,
            error_kw=dict(ecolor="#333", lw=0.8, capsize=2.5))
    for yi, m, lo, hi in zip(y, medians, q25, q75):
        ax.text(hi + 0.04, yi, f"{m:.2f}  [{lo:.2f}, {hi:.2f}]",
                va="center", fontsize=8.5)
    ax.set_yticks(y); ax.set_yticklabels(labels, fontsize=9)
    ax.set_xlabel("Median delay attributed per trip (minutes, whiskers = Q1–Q3)",
                  fontsize=11)
    other_med = np.percentile(other_secs, 50) / 60
    other_q25 = np.percentile(other_secs, 25) / 60
    other_q75 = np.percentile(other_secs, 75) / 60
    ax.set_title(
        f"Naive Delay — Top {TOP} sources across {n_trips} archive Route 22 SB trips\n"
        f"Median (whiskers Q1–Q3); OTHER bucket median "
        f"{other_med:.2f} min/trip [{other_q25:.2f}, {other_q75:.2f}]",
        fontsize=12, pad=10,
    )
    ax.grid(True, axis="x", alpha=0.3, linewidth=0.5)
    handles = [plt.Rectangle((0, 0), 1, 1, color=_kind_color_full(k))
               for k in ("stop", "traffic_signals", "stop_sign",
                          "ped_signal", "ped_marked")]
    ax.legend(handles, ["bus stop", "traffic signal", "stop sign",
                          "ped signal", "crosswalk"],
              loc="lower right", fontsize=9, frameon=True)
    fig.tight_layout()
    out = SLIDES / "H_bar_aggregate_median.png"
    fig.savefig(out, dpi=160, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  saved: {out}")


def slide_H_stem_median(rows, n_trips) -> None:
    """Stem plot using median delay per feature instead of mean."""
    print("[H_stem_median] aggregate stem plot along route (median)…")
    fig, ax = plt.subplots(figsize=(16, 7), dpi=160)
    ax.set_facecolor("white")
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)

    for r in rows:
        c = _kind_color_full(r["kind"])
        ax.vlines(r["d_mi"], 0, r["median_min"], color=c, linewidth=1.6, alpha=0.9)
        ax.scatter([r["d_mi"]], [r["median_min"]], color=c, s=24,
                    edgecolor="black", linewidth=0.4, zorder=4)

    rows_sorted = sorted(rows, key=lambda r: -r["median_min"])[:8]
    for r in rows_sorted:
        short = r["label"].split("@", 1)[-1].strip().replace("Clark & ", "").split("/")[0].strip()
        if not short: short = r["label"]
        ax.annotate(_wrap_label(short, 22),
                     xy=(r["d_mi"], r["median_min"]),
                     xytext=(0, 8), textcoords="offset points",
                     fontsize=8, ha="center", color="#333")

    ax.set_xlabel("Distance along Route 22 SB (mi)", fontsize=11)
    ax.set_ylabel("Median delay attributed per trip (min)", fontsize=11)
    ax.set_title(
        f"Naive Delay — Where delay accumulates along Route 22 SB, {n_trips} trips, median per feature",
        fontsize=12, pad=10,
    )
    ax.set_xlim(0, 11.0)
    ax.set_ylim(bottom=0)
    ax.grid(True, axis="y", alpha=0.3, linewidth=0.5)
    handles = [plt.Rectangle((0, 0), 1, 1, color=_kind_color_full(k))
               for k in ("stop", "traffic_signals", "stop_sign",
                          "ped_signal", "ped_marked")]
    ax.legend(handles, ["bus stop", "traffic signal", "stop sign",
                          "ped signal", "crosswalk"],
              loc="upper right", fontsize=9, frameon=True)
    fig.tight_layout()
    out = SLIDES / "H_stem_along_route_median.png"
    fig.savefig(out, dpi=160, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  saved: {out}")


def slide_H_3d_map_stem(rows, polyline, n_trips) -> None:
    """Mean-delay stem plot over a perspective-tilted route basemap."""
    print("[H_3d_map] perspective stem plot over basemap…")
    import contextily as cx
    from mpl_toolkits.mplot3d import Axes3D  # noqa: F401

    R = 6378137.0

    def merc(lat, lon):
        lat = np.asarray(lat, dtype=float); lon = np.asarray(lon, dtype=float)
        x = R * np.radians(lon)
        y = R * np.log(np.tan(np.pi / 4 + np.radians(lat) / 2))
        return x, y

    sx, sy = merc(polyline[:, 0], polyline[:, 1])
    pad = 0.04
    x_min, x_max = float(sx.min()), float(sx.max())
    y_min, y_max = float(sy.min()), float(sy.max())
    px = (x_max - x_min) * pad
    py = (y_max - y_min) * pad
    x_min -= px; x_max += px; y_min -= py; y_max += py

    img, ext = cx.bounds2img(x_min, y_min, x_max, y_max,
                              source=cx.providers.CartoDB.PositronNoLabels,
                              ll=False)
    if img.shape[2] == 4:
        img = img[:, :, :3]
    img = img.astype(np.float32) / 255.0

    # Crop the basemap (which spans full tile boundaries) down to the route bbox.
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
    surface_z = -0.02 * max_mean_pre  # push below the stem feet
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
    if not pts:
        raise RuntimeError("no plottable features")

    fxs = np.array([float(merc(p[0], p[1])[0]) for p in pts])
    fys = np.array([float(merc(p[0], p[1])[1]) for p in pts])
    heights = np.array([p[2] for p in pts])
    bar_colors = [_kind_color_full(p[3]) for p in pts]
    bar_w = (x_max - x_min) * 0.012
    ax.bar3d(fxs - bar_w / 2, fys - bar_w / 2, np.zeros_like(fxs),
             bar_w, bar_w, heights,
             color=bar_colors, edgecolor="black", linewidth=0.25,
             shade=True, alpha=0.95)

    top = sorted(rows, key=lambda r: -r["mean_min"])[:6]
    for r in top:
        if r.get("lat") is None: continue
        fx, fy = merc(r["lat"], r["lon"])
        short = r["label"].split("@", 1)[-1].strip().replace("Clark & ", "").split("/")[0].strip()
        if not short: short = r["label"]
        ax.text(float(fx) + (x_max - x_min) * 0.005,
                 float(fy),
                 r["mean_min"] * 1.05, short,
                 fontsize=8.5, color="#111", ha="left")

    ax.set_xlim(x_min, x_max)
    ax.set_ylim(y_min, y_max)
    max_mean = max(r["mean_min"] for r in rows)
    ax.set_zlim(0, max_mean * 1.15)

    dx = x_max - x_min
    dy = y_max - y_min
    ax.set_box_aspect((dx * 1.8, dy, max(dx, dy) * 0.55))

    ax.view_init(elev=28, azim=-70)

    ax.set_xticks([]); ax.set_yticks([])
    ax.xaxis.pane.set_visible(False)
    ax.yaxis.pane.set_visible(False)
    ax.zaxis.pane.set_visible(False)
    ax.xaxis.line.set_color((1, 1, 1, 0))
    ax.yaxis.line.set_color((1, 1, 1, 0))
    ax.set_zlabel("Mean delay attributed (min/trip)", fontsize=10)
    ax.grid(False)

    ax.set_title(
        f"Naive Delay — mean per feature ({n_trips} trips)",
        fontsize=12, pad=4,
    )

    handles = [plt.Rectangle((0, 0), 1, 1, color=_kind_color_full(k))
               for k in ("stop", "traffic_signals", "stop_sign",
                          "ped_signal", "ped_marked")]
    ax.legend(handles, ["bus stop", "traffic signal", "stop sign",
                          "ped signal", "crosswalk"],
               loc="upper right", fontsize=9, frameon=True)

    out = SLIDES / "H_3d_map_stem.png"
    fig.savefig(out, dpi=160, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  saved: {out}")


def slide_H_map(rows, polyline, n_trips) -> None:
    print("[H_map] bubble map of delay along corridor…")
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

    # Bubble size proportional to mean_min; min size for visibility
    pts = [(r["lat"], r["lon"], r["mean_min"], r["kind"], r["label"])
           for r in rows if r.get("lat") and r.get("lon")]
    if not pts:
        raise RuntimeError("no plottable features")
    means = np.array([p[2] for p in pts])
    max_mean = means.max()
    min_size, max_size = 18, 700
    sizes = min_size + (means / max_mean) * (max_size - min_size)

    for (lat, lon, mn, kind, lab), sz in zip(pts, sizes):
        x, y = to_merc(np.array([lat]), np.array([lon]))
        ax.scatter(x, y, s=sz, color=_kind_color_full(kind),
                    edgecolor="black", linewidth=0.6, alpha=0.78, zorder=4)

    # Annotate the top 6
    top = sorted(pts, key=lambda p: -p[2])[:6]
    for lat, lon, mn, kind, lab in top:
        x, y = to_merc(np.array([lat]), np.array([lon]))
        short = lab.split("@", 1)[-1].strip().replace("Clark & ", "").split("/")[0].strip() or lab
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
    ax.set_xticks([]); ax.set_yticks([])

    # Custom legend with size scale
    handles = [plt.Rectangle((0, 0), 1, 1, color=_kind_color_full(k))
               for k in ("stop", "traffic_signals", "stop_sign")]
    leg = ax.legend(handles, ["bus stop", "traffic signal", "stop sign"],
                     loc="upper right", fontsize=9, frameon=True)
    ax.add_artist(leg)
    ax.set_title(
        f"Naive Delay — {n_trips} trips\n"
        f"bubble size ∝ mean delay/trip; max = {max_mean:.2f} min",
        fontsize=11, pad=8,
    )
    fig.tight_layout()
    out = SLIDES / "H_map_bubbles.png"
    fig.savefig(out, dpi=160, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  saved: {out}")


def slides_H_visualizations() -> None:
    rows, other_secs, n_trips, poly, dist = _compute_h_data()
    slide_H_bar(rows, other_secs, n_trips)
    slide_H_stem(rows, n_trips)
    slide_H_map(rows, poly, n_trips)


def main() -> None:
    SLIDES.mkdir(exist_ok=True)
    slide_G()
    slide_H()


if __name__ == "__main__":
    main()
