"""Render F4_timespace_alltrips_aligned.png from the already-reconstructed
``out_r2_bw5/trajectories.json`` bundle, skipping the slow re-reconstruction
that the original ``build_alltrip_aligned.py`` performs.

Equivalent figure, faster: reads the serialized PCHIP records, wraps each
in a minimal shim that matches the ``ReconstructedTrip`` shape that
``plot_aligned`` expects (``.t`` array + ``.smoothed.f`` spline), and
delegates to the original plotter.
"""

from __future__ import annotations

import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
from scipy.interpolate import CubicHermiteSpline

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from bus_trajectories.serialize import load_records  # noqa: E402

BUNDLE = REPO / "outputs" / "out_r2_bw5" / "trajectories.json"
OUT = REPO / "figures" / "F4_timespace_alltrips_aligned.png"
MAX_DURATION_MIN = 120.0  # F4-only filter; keeps the figure readable
M_PER_MI = 1609.344


class _Smoothed:
    def __init__(self, f):
        self.f = f


class _ShimRecon:
    def __init__(self, t_knots, x_knots, slopes):
        f = CubicHermiteSpline(t_knots, x_knots, slopes, extrapolate=False)
        self.t = np.asarray(t_knots, dtype=float)
        self.smoothed = _Smoothed(f)


def departure_t_seconds(r) -> float:
    """t=0 reference: last stationary ping before >= 0.03 mi forward progress."""
    thresh_m = 0.03 * M_PER_MI
    ts = r.t
    progress = r.smoothed.f(ts) - r.smoothed.f(ts)[0]
    moving = np.where(progress >= thresh_m)[0]
    if len(moving) == 0:
        return ts[0]
    return ts[max(0, moving[0] - 1)]


def plot_aligned(recons: dict, out_path: Path) -> None:
    """All SB trips aligned to actual departure (formerly in build_alltrip_aligned)."""
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


def main() -> int:
    print(f"Loading bundle: {BUNDLE}")
    records = list(load_records(BUNDLE))
    print(f"Loaded {len(records)} records.")
    max_dur_s = MAX_DURATION_MIN * 60
    kept = [
        r for r in records
        if (r["t_knots"][-1] - r["t_knots"][0]) <= max_dur_s
    ]
    n_drop = len(records) - len(kept)
    print(f"Filtered out {n_drop} trip(s) longer than {MAX_DURATION_MIN:.0f} min; "
          f"keeping {len(kept)} for F4.")
    recons = {
        r["trip_id"]: _ShimRecon(r["t_knots"], r["x_knots"], r["slopes"])
        for r in kept
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    plot_aligned(recons, OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
