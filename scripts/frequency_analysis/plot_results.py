"""Metric figures: frequency on x, agreement on y, one line per method.

    uv run python plot_results.py

Current display choices (2026-08-12): M1/M2 plot MAE (RMSE stays in
summary.csv); speed in ft/s to compare with the paper; knee rings and the
R2 reference mark are off (SHOW_KNEES / SHOW_R2 below; the R2 rows are
still scored and present in the CSVs).

Figures in ``outputs/frequency_analysis/figures/``: M1_position, M2_speed,
M3_doors, M3b_stop_location, M4a/M4b accel, M5_signal_zones, M6_delay_time,
M7_delay_events.
"""

from __future__ import annotations

import textwrap

import matplotlib as mpl
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import config as C

SHOW_KNEES = False
SHOW_R2 = False
SHOW_AVL = True          # marks for the real ~15 s AVL archive feed
AVL_COLORS = {"PCHIP": "#4a3aa7", "VCHIP-ME": "#e34948"}  # slots 7-8


def _draw_marks(ax, marks, y_cap=None):
    """Reference-feed marks at their TRUE cadence (no x-dodge); methods are
    distinguished by marker shape + color; clip + annotate above y_cap."""
    shapes = {"PCHIP": "X", "VCHIP-ME": "P", "TL": "D"}
    for j, (x, y, color, label, meth) in enumerate(marks):
        hollow = meth.endswith("-fleet") or meth == "TL2"
        if hollow:
            mk = "D" if meth == "TL2" else ("o" if meth[:5] == "PCHIP" else "s")
        else:
            mk = shapes.get(meth, "X")
        kw = (dict(facecolors="none", edgecolors=color, lw=1.8)
              if hollow else dict(color=color, lw=0))
        if y_cap is not None and y > y_cap:
            ax.scatter([x], [y_cap], marker=mk, s=95, zorder=6,
                       label=label, **kw)
            ax.annotate(f"^ {y:.1f}", (x, y_cap), textcoords="offset points",
                        xytext=(7, -2 - 11 * j), fontsize=8.5, color=color)
        else:
            ax.scatter([x], [y], marker=mk, s=95, zorder=6, label=label, **kw)

SURFACE, PAGE = "#fcfcfb", "#f9f9f7"
INK, SEC, MUT = "#0b0b0b", "#52514e", "#898781"
GRID, BASE = "#e1e0d9", "#c3c2b7"
COLORS = {"PCHIP": "#2a78d6", "VCHIP-ME": "#eb6834", "LSEG": "#1baf7a",
          "LOCREG-PCHIP": "#eda100", "LOCREG-PCHIP-V": "#e87ba4",
          "V-SPLINE-ME": "#008300"}                   # categorical slots 1-6
R2_COLOR = "#4a3aa7"                                  # slot 7 (marks off)

mpl.rcParams.update({
    "font.family": "sans-serif",
    "font.sans-serif": ["Helvetica Neue", "Arial", "DejaVu Sans"],
    "text.color": INK, "axes.edgecolor": BASE, "axes.labelcolor": SEC,
    "xtick.color": MUT, "ytick.color": MUT, "axes.grid": True,
    "grid.color": GRID, "grid.linewidth": 0.8, "axes.axisbelow": True,
})


def _axes(title: str, sub: str, ylabel: str):
    fig, ax = plt.subplots(figsize=(10, 6), facecolor=PAGE, dpi=160)
    n_lines = len(textwrap.wrap(sub, 118))
    top = 0.85 - 0.028 * max(0, n_lines - 2)
    fig.subplots_adjust(left=0.09, right=0.97, top=top, bottom=0.11)
    ax.set_facecolor(SURFACE)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.tick_params(length=0)
    ax.set_xscale("log", base=2)
    ax.set_xticks(list(C.FREQS_S))
    ax.get_xaxis().set_major_formatter(mpl.ticker.ScalarFormatter())
    ax.set_xlabel("ping interval (s)")
    ax.set_ylabel(ylabel)
    fig.suptitle(title, x=0.09, y=0.965, ha="left",
                 fontsize=13, fontweight="bold", color=INK)
    fig.text(0.09, 0.935, textwrap.fill(sub, 118), fontsize=9.5,
             color=SEC, va="top")
    return fig, ax


def _legend(ax, loc="upper left"):
    ax.legend(frameon=False, fontsize=9, labelcolor=SEC,
              loc=loc, handlelength=1.6)


def knee_freq(g: pd.DataFrame, col: str) -> float | None:
    """Last ladder frequency still meeting the metric's knee rule."""
    rule = C.KNEE_RULES.get(col)
    if rule is None:
        return None
    g = g.sort_values("freq")
    vals = g[col].to_numpy()
    ok = vals >= rule[1] * vals[0] if rule[0] == "rel" else vals <= rule[1]
    if not ok.any():
        return None
    fail = np.flatnonzero(~ok)
    last = len(vals) - 1 if not len(fail) else max(fail[0] - 1, 0)
    return float(g["freq"].to_numpy()[last]) if ok[last] else None


def _draw_knee(ax, g, col, scale, color, size=210):
    if not SHOW_KNEES:
        return
    kf = knee_freq(g, col)
    if kf is None:
        return
    y = float(g.loc[g["freq"] == kf, col].iloc[0]) * scale
    ax.scatter([kf], [y], s=size, facecolors="none", edgecolors=color,
               lw=1.8, zorder=5)


def _draw_r2(ax, r2_row, col, scale, y_cap=None):
    """X mark for the real R2 feed; if beyond y_cap, clip + annotate value."""
    if not SHOW_R2 or r2_row is None or pd.isna(r2_row[col]):
        return
    x, y = float(r2_row["freq"]), float(r2_row[col]) * scale
    label = f"R2 feed ({C.R2_METHOD}, ~{r2_row['freq']:.0f}s)"
    if y_cap is not None and y > y_cap:
        ax.scatter([x], [y_cap], marker="x", s=90, color=R2_COLOR, lw=2.6,
                   zorder=6, label=label)
        ax.annotate(f"^ {y:.0f}", (x, y_cap), textcoords="offset points",
                    xytext=(8, -2), fontsize=8.5, color=R2_COLOR)
    else:
        ax.scatter([x], [y], marker="x", s=90, color=R2_COLOR, lw=2.6,
                   zorder=6, label=label)


def line_plot(df, r2_row, col, scale, title, sub, ylabel, fname, ylim=None,
              legend_loc="upper left", extra_marks=(), methods=None):
    fig, ax = _axes(title, sub, ylabel)
    ladder_max = 0.0
    for i, m in enumerate(methods or C.METHODS):
        g = df[df["method"] == m].sort_values("freq")
        dashed = m.startswith(("LOCREG", "V-SPLINE"))  # dashed on top: where
        ax.plot(g["freq"], g[col] * scale, color=COLORS[m],   # tuned k hits 5
                lw=1.8 if dashed else 2.2,                    # they coincide
                ls=(0, (4, 3)) if dashed else "-",            # with their
                marker="o", ms=0 if dashed else 4.5,          # twins
                label=m, zorder=4 if dashed else 3)
        _draw_knee(ax, g, col, scale, COLORS[m], size=210 + 110 * i)
        ladder_max = max(ladder_max, float(g[col].max()) * scale)
    y_cap = ladder_max * 1.12 if ylim is None else None
    _draw_r2(ax, r2_row, col, scale, y_cap=y_cap)
    _draw_marks(ax, extra_marks, y_cap=y_cap)
    if ylim:
        ax.set_ylim(*ylim)
    _legend(ax, legend_loc)
    fig.savefig(C.FIG_DIR / fname, facecolor=PAGE)
    plt.close(fig)


def plot_doors(df: pd.DataFrame, r2_row, sub_common: str, extra_marks=(),
               methods=None):
    lo_th, hi_th = min(C.STOP_FTPS_WHISKERS), max(C.STOP_FTPS_WHISKERS)
    fig, ax = _axes(
        "Doors-open stopped agreement (M3)",
        f"Dot: % of AVL door-open seconds reconstructed below "
        f"{C.STOP_FTPS_MAIN:.0f} ft/s (at true x). Whisker caps: "
        f"{lo_th:.0f} ft/s (bottom) and {hi_th:.0f} ft/s (top); whiskers "
        f"dodged sideways. {sub_common}",
        "% of door-open seconds below threshold")
    col = f"door_stop_pct_{C.STOP_FTPS_MAIN}"
    mlist = list(methods or C.METHODS)
    n = len(mlist)
    offsets = np.linspace(-0.07, 0.07, n) if n > 1 else [0.0]
    for m, off in zip(mlist, offsets):
        g = df[df["method"] == m].sort_values("freq")
        xw = g["freq"] * (2.0 ** off)
        ax.vlines(xw, g[f"door_stop_pct_{lo_th}"], g[f"door_stop_pct_{hi_th}"],
                  color=COLORS[m], lw=1.1, alpha=0.7, zorder=2)
        for th in C.STOP_FTPS_WHISKERS:
            ax.scatter(xw, g[f"door_stop_pct_{th}"], marker="_", s=26,
                       color=COLORS[m], alpha=0.7, zorder=2, lw=1.1)
        ax.plot(g["freq"], g[col], color=COLORS[m], lw=2.2, marker="o",
                ms=5, label=m, zorder=3)
        _draw_knee(ax, g, col, 1.0, COLORS[m])
    if SHOW_R2 and r2_row is not None:
        xr = r2_row["freq"]
        ax.vlines([xr], r2_row[f"door_stop_pct_{lo_th}"],
                  r2_row[f"door_stop_pct_{hi_th}"],
                  color=R2_COLOR, lw=1.1, alpha=0.8, zorder=2)
        for th in C.STOP_FTPS_WHISKERS:
            ax.scatter([xr], [r2_row[f"door_stop_pct_{th}"]], marker="_",
                       s=26, color=R2_COLOR, alpha=0.8, zorder=2, lw=1.1)
        _draw_r2(ax, r2_row, col, 1.0)
    ax.set_ylim(0, 102)
    _draw_marks(ax, extra_marks)
    _legend(ax, "lower left")
    fig.savefig(C.FIG_DIR / "M3_doors.png", facecolor=PAGE)
    plt.close(fig)


if __name__ == "__main__":
    C.FIG_DIR.mkdir(parents=True, exist_ok=True)
    full = pd.read_csv(C.RESULTS_DIR / "summary.csv")
    df = full[full["method"] != "R2"]
    r2_rows = full[full["method"] == "R2"]
    r2 = r2_rows.iloc[0] if len(r2_rows) else None
    n_trips = int(df["n_trips"].max())
    base = f"{C.BASELINE[0]} @ {C.BASELINE[1]} s"
    # baseline-referenced metrics (M1/M2/M5/M6/M7) name the baseline;
    # ground-truth-referenced ones (M3/M3b/M4) are scored against AVL/physics
    common = f"All {n_trips} complete trips; baseline = {base}."
    common_gt = f"All {n_trips} complete trips; truth = AVL, no baseline involved."

    # AVL-archive reference marks: 3 study vehicles, covered trips only,
    # 2026-06-11 -> 2026-08-07 (same corpus/date range on every figure);
    # x = pooled MEAN inter-ping interval
    avl_marks = {"m1": [], "m2": [], "m3": [], "m4a": [], "m4b": [], "m5": []}
    avl_path = C.RESULTS_DIR / "avl_reference.csv"
    if SHOW_AVL and avl_path.exists():
        adf = pd.read_csv(avl_path)
        if "max_gap_s" in adf:
            adf = adf[adf["max_gap_s"] <= 120.0]
        pings = pd.read_parquet(C.CACHE_DIR / "avl_trip_pings.parquet")
        pings = pings[pings["trip_key"].isin(set(adf["trip_key"]))]
        xa = float(np.concatenate([
            g["ping_dt"].diff().dt.total_seconds().dropna().to_numpy()
            for _, g in pings.groupby("trip_key")]).mean())
        for meth, g in adf.groupby("method"):
            col_, lab = AVL_COLORS[meth], f"AVL feed ({meth}, mean ~{xa:.1f}s)"
            avl_marks["m1"].append((xa, g["ho_mae_x"].mean(), col_, lab, meth))
            avl_marks["m2"].append(
                (xa, g["ho_mae_v"].mean() * C.MPS_TO_FTPS, col_, lab, meth))
            avl_marks["m3"].append(
                (xa, 100 * g[f"door_stop_{C.STOP_FTPS_MAIN}"].sum()
                 / g["door_total_s"].sum(), col_, lab, meth))
            avl_marks["m4a"].append(
                (xa, 100 * g["accel_ok_tight"].sum() / g["n_accel"].sum(),
                 col_, lab, meth))
            avl_marks["m4b"].append(
                (xa, 100 * g["accel_ok_loose"].sum() / g["n_accel"].sum(),
                 col_, lab, meth))
            avl_marks["m5"].append(
                (xa, 100 * g["zone_abs_err_sum"].sum()
                 / g["zone_tt_base_sum"].sum(), col_, lab, meth))

    # TransLink R99 (Vancouver) reference: 1000-trip sample, PCHIP only
    # (feed has no speed channel and no door data -> M1/M4a/M4b only)
    tl_marks = {"m1": [], "m4a": [], "m4b": []}
    tl_path = C.RESULTS_DIR / "translink_reference.csv"
    tl_note = ""
    if SHOW_AVL and tl_path.exists():
        tdf = pd.read_csv(tl_path)
        xt = 26.7   # pooled mean inter-ping interval (translink_eval output)
        lab_t = f"TransLink R99 (PCHIP, mean ~{xt:.0f}s)"
        tl_marks["m1"].append((xt, tdf["ho_mae_x"].mean(), INK, lab_t, "TL"))
        tl_marks["m4a"].append(
            (xt, 100 * tdf["accel_ok_tight"].sum() / tdf["n_accel"].sum(),
             INK, lab_t, "TL"))
        tl_marks["m4b"].append(
            (xt, 100 * tdf["accel_ok_loose"].sum() / tdf["n_accel"].sum(),
             INK, lab_t, "TL"))
        tl_note = (" Diamonds = TransLink Vancouver, 1000-trip samples, "
                   "position-only GTFS-rt: filled = R99 Broadway "
                   "(2026-06-23 to 07-28), hollow = R4 41st Ave, open-sky "
                   "corridor (2026-06-23 to 08-26).")
        tl4 = C.RESULTS_DIR / "translink_r4_reference.csv"
        if tl4.exists():
            t4 = pd.read_csv(tl4)
            x4 = float(t4["cadence_s"].mean())
            lab4 = f"TransLink R4 (PCHIP, mean ~{x4:.0f}s)"
            tl_marks["m1"].append((x4, t4["ho_mae_x"].mean(), INK, lab4, "TL2"))
            tl_marks["m4a"].append(
                (x4, 100 * t4["accel_ok_tight"].sum() / t4["n_accel"].sum(),
                 INK, lab4, "TL2"))
            tl_marks["m4b"].append(
                (x4, 100 * t4["accel_ok_loose"].sum() / t4["n_accel"].sum(),
                 INK, lab4, "TL2"))

    CORE = ("PCHIP", "VCHIP-ME", "LSEG")      # M1/M3/M5 show the core trio;
    FULL = C.METHODS                          # M2/M4a/M4b show everything
    avl_note = ("AVL marks: buses 1566/8089/8099, covered trips "
                "2026-06-11 to 2026-08-07, mean inter-ping interval 17.1 s "
                "(same feed and date range on every figure).")

    ho = (f"5% holdout validation: reconstruction from the remaining 95% of "
          f"knots, scored against the MEASURED position/speed at the held-out "
          f"2 s pings (same points at every frequency; RMSE in summary.csv). "
          f"All {n_trips} complete trips. X and + marks = real AVL-archive "
          f"feed scored on 5% of its OWN held-out pings. {avl_note}")
    line_plot(df, r2, "ho_mae_x_mean", 1.0,
              "Positional agreement vs ping interval (M1)",
              f"Mean per-trip position MAE at held-out pings. {ho}{tl_note}",
              "position MAE (m)", "M1_position.png",
              extra_marks=avl_marks["m1"] + tl_marks["m1"], methods=CORE)
    line_plot(df, r2, "ho_mae_v_mean", C.MPS_TO_FTPS,
              "Speed agreement vs ping interval (M2)",
              f"Mean per-trip speed MAE at held-out pings, in ft/s for "
              f"comparison with the paper. {ho} TransLink omitted: no speed channel.",
              "speed MAE (ft/s)", "M2_speed.png",
              extra_marks=avl_marks["m2"], methods=FULL)
    plot_doors(df, r2, common_gt + " X and + = real AVL-archive feed. "
               + avl_note + " TransLink omitted: no door data.",
               extra_marks=avl_marks["m3"], methods=CORE)
    line_plot(df, r2, "accel_tight_pct", 1.0,
              "Reasonable acceleration — tight bounds (M4a)",
              f"% of grid seconds with acceleration in [{C.ACCEL_TIGHT[0]:.2f}, "
              f"{C.ACCEL_TIGHT[1]:.2f}] m/s² (paper tight bounds). Realism "
              f"check, no baseline involved. All {n_trips} complete trips. "
              f"X and + = real AVL-archive feed. {avl_note}{tl_note}",
              "% of seconds within bounds", "M4a_accel_tight.png",
              legend_loc="center right", methods=FULL,
              extra_marks=avl_marks["m4a"] + tl_marks["m4a"])
    line_plot(df, r2, "accel_loose_pct", 1.0,
              "Reasonable acceleration — loose bounds (M4b)",
              f"% of grid seconds with acceleration in [{C.ACCEL_LOOSE[0]:.2f}, "
              f"{C.ACCEL_LOOSE[1]:.2f}] m/s² (paper loose bounds). Realism "
              f"check, no baseline involved. All {n_trips} complete trips. "
              f"X and + = real AVL-archive feed. {avl_note}{tl_note}",
              "% of seconds within bounds", "M4b_accel_loose.png",
              legend_loc="center right", methods=FULL,
              extra_marks=avl_marks["m4b"] + tl_marks["m4b"])
    line_plot(df, r2, "zone_wmape_pct", 1.0,
              "Signal-zone travel time error (M5)",
              "Travel time through the 300 ft upstream of each signalized "
              f"intersection; weighted MAPE vs baseline. {common} "
              f"X and + = real AVL-archive feed. {avl_note}",
              "travel-time error (% of baseline)", "M5_signal_zones.png",
              extra_marks=avl_marks["m5"], methods=CORE)
    print("figures ->", C.FIG_DIR)
