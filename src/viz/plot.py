"""Diagnostic plots for trip reconstructions."""

from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

from core.pipeline import TripReconstruction

_M_PER_MI = 1609.344


def plot_time_space(
    recons: dict[str, TripReconstruction],
    out_path: str | Path,
    title: str = "Time-space diagram",
    n_eval: int = 1000,
):
    """Smoothed `f(t)` (line) over raw on-route pings (dots), one trip per color."""
    fig, ax = plt.subplots(figsize=(13, 7))
    colors = plt.cm.tab10.colors

    items = sorted(recons.items(), key=lambda kv: kv[1].meta.first_ping)
    for i, (trip_id, r) in enumerate(items):
        c = colors[i % 10]
        # Raw dots in absolute clock time.
        raw_times = r.meta.first_ping + np.array([np.timedelta64(int(s * 1000), "ms") for s in r.t])
        ax.scatter(raw_times, r.d / _M_PER_MI, s=4, color=c, alpha=0.4)
        # Smoothed line.
        ts = np.linspace(r.t[0], r.t[-1], n_eval)
        xs = r.smoothed.f(ts) / _M_PER_MI
        smooth_times = r.meta.first_ping + np.array(
            [np.timedelta64(int(s * 1000), "ms") for s in ts]
        )
        ax.plot(
            smooth_times,
            xs,
            "-",
            color=c,
            linewidth=1.4,
            label=f"{trip_id} (bus {r.meta.bus_id})",
        )

    ax.set_xlabel("Time of day")
    ax.set_ylabel("Distance along route (mi)")
    ax.set_title(title)
    ax.grid(True, alpha=0.3)
    ax.legend(loc="best", fontsize=8, ncol=2)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(out_path, dpi=140)
    plt.close(fig)


def plot_speed_profile(
    recon: TripReconstruction,
    out_path: str | Path,
    n_eval: int = 1000,
):
    """Speed (mph) vs distance (mi) along the route for a single trip."""
    f = recon.smoothed.f
    ts = np.linspace(recon.t[0], recon.t[-1], n_eval)
    d_m = f(ts)
    v_mps = f.derivative()(ts)  # m/s
    v_mph = v_mps * 2.23694
    fig, ax = plt.subplots(figsize=(11, 5))
    ax.plot(d_m / _M_PER_MI, v_mph, linewidth=1.2)
    ax.set_xlabel("Distance along route (mi)")
    ax.set_ylabel("Speed (mph)")
    ax.set_title(
        f"Speed profile — trip {recon.meta.trip_id} (bus {recon.meta.bus_id})"
    )
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_path, dpi=140)
    plt.close(fig)
