"""Render the agreement-vs-ping-frequency curves.

One panel per metric: pooled (micro-averaged) agreement across all complete
trips, with the interquartile range across trips as a band. X axis = median
ping interval (log2). Every curve starts at 100% at the 2 s benchmark —
M6/M7 are graded against external AVL door data, so they are normalized to
their benchmark value (raw values annotated).

    PYTHONPATH=src uv run python scripts/frequency_analysis/plot_results.py
"""

from __future__ import annotations

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import config as C  # noqa: E402

ACCENT = "#1f77b4"          # repo curve palette [0]
BAND = "#1f77b4"
KNEE = "#d62728"            # repo curve palette [3]
GRID_KW = dict(alpha=0.25, lw=0.7)

METRICS = [
    # (per-trip col, summary col, title, subtitle, normalize_to_benchmark)
    ("m1", "M1_position_pct", "M1 · Position agreement",
     f"% of seconds within {C.POS_TOL_M:.0f} m of the benchmark trajectory", False),
    ("m2", "M2_speed_pct", "M2 · Speed agreement",
     f"% of seconds within {C.SPEED_TOL_MPH:.1f} mph of the benchmark speed", False),
    ("m3", "M3_slowstate_f1", "M3 · Slow-state detection (per-second F1)",
     f"F1 of the v<{C.SLOW_MPH:.0f} mph mask vs. benchmark, 1 Hz", False),
    ("m4", "M4_event_f1", "M4 · Delay-event detection (event-level F1)",
     f"F1 of detected slowdown events (≥{C.MIN_EVENT_S:.0f} s), matched by overlap", False),
    ("m5", "M5_attribution_jaccard", "M5 · Delay attribution agreement",
     "weighted Jaccard of delay seconds by (category, facility)", False),
    ("m6", "M6_door_speed_pct", "M6 · AVL door-open speed consistency",
     "% of AVL door-open seconds reconstructed below 5 mph (raw)", False),
    ("m7", "M7_dwell_recall_pct", "M7 · AVL serviced-stop dwell recall",
     "% of AVL serviced stops recovered as dwell at that stop (raw)", False),
]


def _x(levels: np.ndarray) -> np.ndarray:
    return np.array([C.level_cadence_s(int(l)) for l in levels])


def _style(ax, subtitle: str):
    ax.set_xscale("log", base=2)
    cad = [C.level_cadence_s(l) for l in range(C.N_LEVELS)]
    ax.set_xticks(cad)
    ax.set_xticklabels([f"{c:.0f}" for c in cad])
    ax.set_xlabel("median ping interval (s)")
    ax.set_ylabel("agreement (%)")
    ax.set_ylim(-3, 105)
    ax.grid(True, which="major", **GRID_KW)
    ax.spines[["top", "right"]].set_visible(False)
    ax.set_title(subtitle, fontsize=9, color="#555555", loc="left", pad=2)


FAITHFUL_FRAC = 0.90


def _knee(x: np.ndarray, y: np.ndarray) -> int | None:
    """Index of the last point still >= 90% *of the benchmark value* — the
    edge of the cliff. (For M1–M5 the benchmark is 100 by construction, so
    this is simply >=90%; for raw M6/M7 it is relative to their own start.)

    Curves here are monotone-ish decreasing; the actionable statement is
    "faithful through X s", so the knee is the last level above threshold
    (None when it's the final level — nothing to mark)."""
    above = np.where(y >= FAITHFUL_FRAC * y[0])[0]
    if len(above) == 0 or above[-1] == len(y) - 1:
        return None
    return int(above[-1])


def plot_metric(summary, per_trip, mcol, scol, title, subtitle, normalize, out):
    s = summary.sort_values("level")
    x = _x(s["level"].to_numpy())
    y = s[scol].to_numpy(dtype=float)
    base = y[0]
    if normalize:
        y = 100 * y / base

    fig, ax = plt.subplots(figsize=(7.2, 4.4), dpi=200)
    _style(ax, subtitle)

    # IQR band across trips (normalized per-trip for M6/M7)
    if mcol in per_trip.columns:
        pt = per_trip[["trip_key", "level", mcol]].dropna()
        if normalize:
            b0 = pt[pt.level == 0].set_index("trip_key")[mcol]
            pt = pt.assign(v=100 * pt[mcol] / pt.trip_key.map(b0)).dropna(subset=["v"])
            pt = pt[np.isfinite(pt.v)]
        else:
            pt = pt.assign(v=pt[mcol])
        q = pt.groupby("level")["v"].quantile([0.25, 0.75]).unstack()
        q = q.reindex(s["level"])
        ax.fill_between(x, q[0.25].clip(0, 105), q[0.75].clip(0, 105),
                        color=BAND, alpha=0.14, lw=0, label="IQR across trips")

    ax.plot(x, y, "-o", color=ACCENT, lw=2, ms=5, label="pooled (all trips)")

    k = _knee(x, y)
    if k is not None:
        ax.plot(x[k], y[k], "o", ms=11, mfc="none", mec=KNEE, mew=1.8, zorder=5)
        ax.annotate(f"≥90% of benchmark through {x[k]:.0f} s", (x[k], y[k]),
                    textcoords="offset points", xytext=(8, -16),
                    color=KNEE, fontsize=9, fontweight="bold")

    for xi, yi in zip(x, y):
        ax.annotate(f"{yi:.0f}", (xi, yi), textcoords="offset points",
                    xytext=(0, 7), ha="center", fontsize=7.5, color="#333333")

    extra = f"   (benchmark raw = {base:.1f}%)" if normalize else ""
    ax.set_title(title + extra, fontsize=12, loc="left", pad=18)
    ax.legend(loc="lower left", fontsize=8, frameon=False)
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)
    print(f"  -> {out.name}")


PRECISION_COLOR = "#ff7f0e"  # repo curve palette [1]


def plot_m7(summary, per_trip, out):
    """M7 as recall + precision on one panel (both raw, vs AVL door data)."""
    s = summary.sort_values("level")
    x = _x(s["level"].to_numpy())
    series = [
        ("M7_dwell_recall_pct", "m7", "recall", ACCENT, 8),
        ("M7_dwell_precision_pct", "m7p", "precision", PRECISION_COLOR, -15),
    ]
    fig, ax = plt.subplots(figsize=(7.2, 4.4), dpi=200)
    _style(ax, "recall: AVL serviced stops (type 3, dwell ≥10 s) recovered at that stop · "
               "precision: claimed dwells backed by door activity (types 3/4/5, ≥5 s)")
    for scol, mcol, label, color, dy in series:
        y = s[scol].to_numpy(dtype=float)
        q = (per_trip[["trip_key", "level", mcol]].dropna()
             .groupby("level")[mcol].quantile([0.25, 0.75]).unstack()
             .reindex(s["level"]))
        ax.fill_between(x, q[0.25].clip(0, 105), q[0.75].clip(0, 105),
                        color=color, alpha=0.10, lw=0)
        ax.plot(x, y, "-o", color=color, lw=2, ms=5, label=label)
        k = _knee(x, y)
        if k is not None:
            ax.plot(x[k], y[k], "o", ms=11, mfc="none", mec=KNEE, mew=1.8, zorder=5)
            ax.annotate(f"≥90% of start through {x[k]:.0f} s", (x[k], y[k]),
                        textcoords="offset points", xytext=(10, 2 * dy),
                        color=KNEE, fontsize=8.5, fontweight="bold")
        for xi, yi in zip(x, y):
            ax.annotate(f"{yi:.0f}", (xi, yi), textcoords="offset points",
                        xytext=(0, dy), ha="center", fontsize=7.5, color=color)
    ax.set_title("M7 · AVL serviced-stop dwell — recall & precision (raw)",
                 fontsize=12, loc="left", pad=18)
    ax.legend(loc="lower left", fontsize=8.5, frameon=False)
    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)
    print(f"  -> {out.name}")


def plot_overview(summary, out):
    """Small-multiples grid: all seven metrics + reconstruction success."""
    s = summary.sort_values("level")
    x = _x(s["level"].to_numpy())
    fig, axes = plt.subplots(2, 4, figsize=(16, 7.2), dpi=200, sharex=True, sharey=True)
    panels = [(m, sc, t, n) for m, sc, t, _sub, n in METRICS]
    panels.append((None, "recon_success_rate", "Reconstruction success rate", False))
    for ax, (mcol, scol, title, normalize) in zip(axes.flat, panels):
        y = s[scol].to_numpy(dtype=float)
        if scol == "recon_success_rate":
            y = 100 * y
        elif normalize:
            y = 100 * y / y[0]
        ax.plot(x, y, "-o", color=ACCENT, lw=1.8, ms=3.5,
                label="recall" if mcol == "m7" else None)
        k = _knee(x, y)
        if k is not None:
            ax.plot(x[k], y[k], "o", ms=9, mfc="none", mec=KNEE, mew=1.5)
        if mcol == "m7" and "M7_dwell_precision_pct" in s.columns:
            yp = s["M7_dwell_precision_pct"].to_numpy(dtype=float)
            ax.plot(x, yp, "-o", color=PRECISION_COLOR, lw=1.8, ms=3.5, label="precision")
            kp = _knee(x, yp)
            if kp is not None:
                ax.plot(x[kp], yp[kp], "o", ms=9, mfc="none", mec=KNEE, mew=1.5)
            ax.legend(loc="lower left", fontsize=7, frameon=False)
        ax.set_xscale("log", base=2)
        ax.set_xticks(x)
        ax.set_xticklabels([f"{c:.0f}" for c in x], fontsize=7)
        ax.set_ylim(-3, 105)
        ax.grid(True, **GRID_KW)
        ax.spines[["top", "right"]].set_visible(False)
        ax.set_title(title, fontsize=9.5, loc="left")
    for ax in axes[1]:
        ax.set_xlabel("ping interval (s)", fontsize=8)
    for ax in axes[:, 0]:
        ax.set_ylabel("agreement (%)", fontsize=8)
    fig.suptitle("Trajectory-reconstruction fidelity vs. GPS ping frequency — "
                 "3 CTA buses, 2026-06-11..17, benchmark = 2 s VTRAK feed",
                 fontsize=12, x=0.01, ha="left")
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    fig.savefig(out)
    plt.close(fig)
    print(f"  -> {out.name}")


def main() -> int:
    C.FIG_DIR.mkdir(parents=True, exist_ok=True)
    summary = pd.read_csv(C.RESULTS_DIR / "summary.csv")
    per_trip = pd.read_csv(C.RESULTS_DIR / "per_trip_metrics.csv")
    for mcol, scol, title, subtitle, normalize in METRICS:
        if mcol == "m7" and "M7_dwell_precision_pct" in summary.columns:
            plot_m7(summary, per_trip, C.FIG_DIR / "M7_m7.png")
            continue
        plot_metric(summary, per_trip, mcol, scol, title, subtitle, normalize,
                    C.FIG_DIR / f"{scol.split('_')[0]}_{mcol}.png")
    plot_overview(summary, C.FIG_DIR / "overview.png")
    print(f"figures -> {C.FIG_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
