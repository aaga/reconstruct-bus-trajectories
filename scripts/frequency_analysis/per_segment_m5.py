"""Per-segment attribution agreement (M5 at the segment ledger level).

For every signal-to-signal segment of every trip, compare the *number of
seconds* of delay each reconstruction assigns to the two facility categories
that matter for planning:

    bus stop  = SegmentDecomp.t_dwell   (dwell, incl. near-signal dwell)
    signal    = SegmentDecomp.d_signal  (uniform + overflow)

These buckets come straight from ``core.decompose.decompose_trip`` — events
spanning segment boundaries are duration-clipped into each segment, exactly
as the dashboard/aggregate figures use them.

Only segments fully traversed by BOTH reconstructions are compared (the
variant's domain is slightly shorter at trip edges).

Agreement per category (the knee curve) is the pooled weighted ratio
    sum_seg min(bench_sec, var_sec) / sum_seg max(bench_sec, var_sec)
plus MAE (s/segment, over segments where either side attributed >0 s) as a
secondary error magnitude.

    PYTHONPATH=src uv run python scripts/frequency_analysis/per_segment_m5.py
        [--no-recompute]   reuse results/per_segment_m5.csv, just replot
"""

from __future__ import annotations

import argparse

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import config as C  # noqa: E402
import pipeline as P  # noqa: E402
import trip_index as TI  # noqa: E402

# Repo semantic colors (src/viz/colors.py KIND_COLOR): stop=blue, signal=orange.
COLORS = {"busstop": "#3a85d6", "signal": "#e5896a"}
LABELS = {"busstop": "bus-stop dwell", "signal": "signal delay"}
KNEE = "#d62728"
FAITHFUL_FRAC = 0.90


def compute() -> pd.DataFrame:
    trips = TI.load_cached_trips()
    rows = []
    for i, trip in enumerate(trips, 1):
        bench = P.run_level(trip, 0)
        if not bench.seg_buckets:
            continue
        bench_by_id = {s["seg_id"]: s for s in bench.seg_buckets}
        for level in range(C.N_LEVELS):
            var = bench if level == 0 else P.run_level(trip, level)
            x_lo = max(bench.x[0], var.x[0])
            x_hi = min(bench.x[-1], var.x[-1])
            for s in var.seg_buckets:
                b = bench_by_id[s["seg_id"]]
                # full-coverage filter: segment inside both traversed spans
                if s["x_start_m"] < x_lo or s["x_end_m"] > x_hi:
                    continue
                rows.append({
                    "trip_key": trip.trip_key, "route_id": trip.route_id,
                    "level": level, "seg_id": s["seg_id"],
                    "length_m": s["x_end_m"] - s["x_start_m"],
                    "bench_busstop_s": b["t_dwell_s"], "var_busstop_s": s["t_dwell_s"],
                    "bench_signal_s": b["d_signal_s"], "var_signal_s": s["d_signal_s"],
                })
        if i % 25 == 0:
            print(f"  {i}/{len(trips)}")
    df = pd.DataFrame(rows)
    df["err_busstop_s"] = (df.var_busstop_s - df.bench_busstop_s).abs()
    df["err_signal_s"] = (df.var_signal_s - df.bench_signal_s).abs()
    C.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(C.RESULTS_DIR / "per_segment_m5.csv", index=False)
    print(f"wrote {C.RESULTS_DIR / 'per_segment_m5.csv'} ({len(df)} segment rows)")
    return df


def summarize(df: pd.DataFrame) -> pd.DataFrame:
    out = []
    for level, g in df.groupby("level"):
        row = {"level": level, "cadence_s": C.level_cadence_s(int(level)),
               "n_segments": len(g)}
        for cat in ("busstop", "signal"):
            b = g[f"bench_{cat}_s"].to_numpy()
            v = g[f"var_{cat}_s"].to_numpy()
            row[f"{cat}_agree_pct"] = 100 * np.minimum(b, v).sum() / max(np.maximum(b, v).sum(), 1e-9)
            nz = (b > 0) | (v > 0)
            row[f"{cat}_mae_s"] = float(np.abs(v - b)[nz].mean()) if nz.any() else 0.0
            row[f"{cat}_bench_total_s"] = float(b.sum())
            row[f"{cat}_var_total_s"] = float(v.sum())
        out.append(row)
    s = pd.DataFrame(out).sort_values("level").reset_index(drop=True)
    s.to_csv(C.RESULTS_DIR / "per_segment_m5_summary.csv", index=False)
    return s


def plot(s: pd.DataFrame) -> None:
    C.FIG_DIR.mkdir(parents=True, exist_ok=True)
    x = s["cadence_s"].to_numpy()
    fig, ax = plt.subplots(figsize=(7.2, 4.4), dpi=200)
    ax.set_xscale("log", base=2)
    ax.set_xticks(x)
    ax.set_xticklabels([f"{c:.0f}" for c in x])
    ax.set_xlabel("median ping interval (s)")
    ax.set_ylabel("agreement in attributed seconds (%)")
    ax.set_ylim(-3, 105)
    ax.grid(True, alpha=0.25, lw=0.7)
    ax.spines[["top", "right"]].set_visible(False)

    dodge = {"busstop": 8, "signal": -15}  # keep the two series' labels apart
    for cat in ("busstop", "signal"):
        y = s[f"{cat}_agree_pct"].to_numpy()
        ax.plot(x, y, "-o", color=COLORS[cat], lw=2, ms=5, label=LABELS[cat])
        above = np.where(y >= FAITHFUL_FRAC * y[0])[0]
        if len(above) and above[-1] < len(y) - 1:
            k = above[-1]
            ax.plot(x[k], y[k], "o", ms=11, mfc="none", mec=KNEE, mew=1.8, zorder=5)
            ax.annotate(f"≥90% through {x[k]:.0f} s", (x[k], y[k]),
                        textcoords="offset points", xytext=(10, dodge[cat] * 2),
                        color=KNEE, fontsize=8.5, fontweight="bold")
        for xi, yi in zip(x, y):
            ax.annotate(f"{yi:.0f}", (xi, yi), textcoords="offset points",
                        xytext=(0, dodge[cat]), ha="center", fontsize=7.5,
                        color=COLORS[cat])

    ax.set_title("Per-segment delay-seconds agreement — bus-stop dwell vs. signal\n"
                 "Σmin/Σmax of attributed seconds per signal-to-signal segment, 123 trips",
                 fontsize=10.5, loc="left", pad=10)
    ax.legend(loc="lower left", fontsize=8.5, frameon=False)
    fig.tight_layout()
    out = C.FIG_DIR / "M5seg_per_segment.png"
    fig.savefig(out)
    plt.close(fig)
    print(f"  -> {out}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-recompute", action="store_true")
    args = ap.parse_args()
    if args.no_recompute and (C.RESULTS_DIR / "per_segment_m5.csv").exists():
        df = pd.read_csv(C.RESULTS_DIR / "per_segment_m5.csv")
    else:
        df = compute()
    s = summarize(df)
    print(s.round(2).to_string(index=False))
    plot(s)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
