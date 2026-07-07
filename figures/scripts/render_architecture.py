"""Render ARCHITECTURE.png — the repo's layer/dependency diagram.

Kept in-tree so the diagram stays reproducible and current. Run:

    uv run python figures/scripts/render_architecture.py
"""

from __future__ import annotations

from pathlib import Path

import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "ARCHITECTURE.png"

# palette per layer
C = {
    "dataio": "#dbe7f6",
    "core": "#d7efdd",
    "analysis": "#fce7cf",
    "figures": "#e7ddf6",
    "dashboard": "#f7dada",
    "rec": "#e6e6e6",
    "src": "#ffffff",
}
EDGE = {
    "dataio": "#4a78b5", "core": "#3f9d5a", "analysis": "#d08a3a",
    "figures": "#7b5aa6", "dashboard": "#c05a5a", "rec": "#888888", "src": "#bbbbbb",
}


def box(ax, x, y, w, h, key, title, lines, *, title_size=11, tag=None):
    ax.add_patch(FancyBboxPatch(
        (x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.08",
        linewidth=1.6, edgecolor=EDGE[key], facecolor=C[key], zorder=2))
    ax.text(x + w / 2, y + h - 0.28, title, ha="center", va="top",
            fontsize=title_size, fontweight="bold", color="#222", zorder=3)
    if tag:
        ax.text(x + 0.12, y + h - 0.16, tag, ha="left", va="top",
                fontsize=9, color=EDGE[key], fontweight="bold", zorder=3)
    ax.text(x + w / 2, y + h - 0.62, "\n".join(lines), ha="center", va="top",
            fontsize=8, color="#333", zorder=3, linespacing=1.35)


def arrow(ax, xy1, xy2, color="#666", style="-|>", ls="-"):
    ax.add_patch(FancyArrowPatch(
        xy1, xy2, arrowstyle=style, mutation_scale=13, linewidth=1.3,
        color=color, linestyle=ls, shrinkA=2, shrinkB=2, zorder=1))


def main() -> int:
    fig, ax = plt.subplots(figsize=(13, 9.2))
    ax.set_xlim(0, 20)
    ax.set_ylim(0, 15)
    ax.axis("off")

    ax.text(10, 14.6, "reconstruct-bus-trajectories — architecture",
            ha="center", fontsize=16, fontweight="bold")
    ax.text(10, 14.15, "solid = data / build flow ('feeds');  dashed = results;  colour = layer",
            ha="center", fontsize=9, color="#777")

    # --- trace sources (top) ---
    srcs = ["R2 AVL archive", "Record-a-Ride\nPages backup API", "phone GPS CSV", "VTRAK list"]
    for i, s in enumerate(srcs):
        x = 0.6 + i * 3.7
        box(ax, x, 13.0, 3.3, 0.95, "src", s, [], title_size=9)
        arrow(ax, (x + 1.65, 13.0), (x + 1.65, 12.05), color="#4a78b5")
    ax.text(14.9, 13.3, "GPS-trace sources", fontsize=9, style="italic", color="#4a78b5")

    # --- dataio ---
    box(ax, 0.6, 9.7, 14.3, 2.3, "dataio", "dataio/  —  external I/O  (network · files)",
        ["gtfs · realtime (unified R2 client) · intersections · way_match · vtrak · records_io",
         "sources/  →  trace adapters  →  canonical trace"], tag="I/O")
    ax.text(2.2, 10.15, "canonical trace = { timestamp, lat, lon, trip_id, route_id, pattern_id }",
            fontsize=7.5, style="italic", color="#4a78b5",
            bbox=dict(boxstyle="round,pad=0.2", fc="white", ec="#9bb7d8", lw=0.8))

    # --- core ---
    box(ax, 0.6, 7.05, 14.3, 2.0, "core", "src/core/  —  pure business logic  (no I/O, no plotting)",
        ["smooth (LOCREG/PCHIP) · reconstruct · mapmatch · serialize · control_points",
         "decompose/ (events · dwell · attribution · loss · segments · travel_time · feature_attribution)"],
        tag="core")

    # --- analysis ---
    box(ax, 0.6, 4.35, 14.3, 2.05, "analysis", "analysis/  —  results, prep helpers & payloads",
        ["prep/ (geometry · bands · speed · facility) · run_decomposition · data_prep/",
         "comparison.py (phone+R2+AVL fusion)  ·  build_dashboard_data (unified builder)"],
        tag="analysis")

    # --- figures + dashboard (side by side) ---
    box(ax, 0.6, 1.35, 6.9, 2.35, "figures", "figures/  —  visualization",
        ["viz/ (plot · timespace · compare · colors)",
         "scripts/  →  ALL figure scripts, one place",
         "→ figures/<family>.png  (A–H)"], tag="figures")
    box(ax, 8.0, 1.35, 6.9, 2.35, "dashboard", "dashboard/  —  one merged app",
        ["app/ shared JS + views/  ·  map · state · street-view",
         "Single trip (Trajectory · Speed) · Average trip (Overall · Segment)",
         "data/ catalog (trips + aggregates)"], tag="dashboard")

    # --- record-a-ride (right rail) ---
    box(ax, 15.4, 7.05, 4.0, 4.95, "rec", "record-a-ride/",
        ["recording front-end",
         "(app.js · providers/ · sensors)", "",
         "functions/ (Cloudflare Pages API)", "wrangler.toml · scripts/", "",
         "— separate live deploy —"], title_size=10, tag="app")

    # --- corridor note ---
    ax.text(15.4, 6.5, "src/corridor.py — one config for the study route (22 SB); scripts read it",
            fontsize=7.5, style="italic", color="#777", ha="left")

    # --- flow arrows ---
    arrow(ax, (7.75, 9.7), (7.75, 9.05), color="#4a78b5")       # dataio -> core
    ax.text(6.0, 9.35, "canonical trace", fontsize=7.5, style="italic", color="#4a78b5")
    arrow(ax, (7.75, 7.05), (7.75, 6.4), color="#3f9d5a")       # core -> analysis
    arrow(ax, (4.0, 4.35), (4.0, 3.7), color="#d08a3a", ls="--")   # analysis -> figures
    ax.text(3.2, 4.0, "results", fontsize=7.5, style="italic", color="#d08a3a")
    arrow(ax, (11.5, 4.35), (11.5, 3.7), color="#d08a3a")          # analysis -> dashboard
    ax.text(9.8, 4.0, "build_dashboard_data → data/", fontsize=7.5, style="italic", color="#d08a3a")
    arrow(ax, (2.2, 13.0), (2.2, 12.05), color="#4a78b5")
    # record-a-ride feeds the Pages-backup source
    arrow(ax, (15.4, 9.5), (12.0, 11.3), color="#888888", ls="--")
    ax.text(12.3, 12.4, "serves\nPages backup", fontsize=7, style="italic", color="#888", ha="left")

    # --- legend ---
    handles = [mpatches.Patch(facecolor=C[k], edgecolor=EDGE[k],
                              label=lbl) for k, lbl in [
        ("core", "① core"), ("dataio", "dataio"), ("analysis", "② analysis (+prep)"),
        ("figures", "③ figures"), ("dashboard", "④ dashboard"), ("rec", "record-a-ride")]]
    ax.legend(handles=handles, loc="lower center", ncol=6, fontsize=8.5,
              frameon=False, bbox_to_anchor=(0.5, -0.02))

    fig.savefig(OUT, dpi=130, bbox_inches="tight", facecolor="white")
    print(f"[architecture] wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
