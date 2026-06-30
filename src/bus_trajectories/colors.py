"""Shared color palettes for figures, dashboards, and interactive viewers.

Previously these constants were copy-pasted across ``viz.py``, ``viz_compare.py``,
``plot_intersections.py`` and the decomposition figure scripts. They live here now
so a palette change happens in one place.
"""

from __future__ import annotations

# --- Trip / curve palette (matplotlib "tab10") -----------------------------
# Used to color a set of trips or bandwidth curves in the interactive viewers.
CURVE_PALETTE = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
    "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
]


def curve_color(i: int) -> str:
    """The i-th curve color, wrapping around the palette."""
    return CURVE_PALETTE[i % len(CURVE_PALETTE)]


# --- Control-point (intersection layer) colors -----------------------------
CONTROL_TYPE_COLORS = {
    "traffic_signals": "#dc8c32",       # amber circle
    "stop": "#cc0000",                  # red octagon
    "ped_crossing_signal": "#7b3fa0",   # purple circle — signalised crosswalk
    "ped_crossing_marked": "#00897b",   # teal circle — marked crosswalk
}


# --- Delay-decomposition category colors -----------------------------------
# Per-event attribution categories (speed-profile figure).
COL_DWELL = "#7a9cc0"            # blue
COL_DWELL_NEAR = "#5276a3"       # darker blue — dwell at a near-side stop
COL_SIGNAL_UNIFORM = "#e5896a"   # warm orange
COL_SIGNAL_OVERFLOW = "#a8492a"  # darker orange/red
COL_CROSSING = "#d6b56a"         # mustard
COL_SLOWDOWN = "#b27ab2"         # purple — unattributed slowdown
COL_LOSS = "#cccccc"             # grey — accel/decel shoulder
COL_SEG_POS = "#dceedb"
COL_SEG_NEG = "#f6d0c8"


def color_for_attribution(category: str, dwell_near_signal: bool = False) -> str:
    """Color for a delay-attribution category (see ``EventAttribution.category``)."""
    if category == "dwell":
        return COL_DWELL_NEAR if dwell_near_signal else COL_DWELL
    if category == "crossing":
        return COL_CROSSING
    if category == "signal_uniform":
        return COL_SIGNAL_UNIFORM
    if category == "signal_overflow":
        return COL_SIGNAL_OVERFLOW
    return COL_SLOWDOWN


# --- Facility-kind colors (corridor attribution figures) -------------------
KIND_COLOR = {
    "stop": "#3a85d6",            # bus stop — blue
    "stop_near_side": "#5276a3",  # near-side bus stop — darker blue
    "signal": "#e5896a",          # traffic / ped signal — warm orange
    "crossing": "#d6b56a",        # marked crossing / stop sign — mustard
}
KIND_LABEL = {
    "stop": "bus stop",
    "stop_near_side": "near-side stop",
    "signal": "signal",
    "crossing": "crossing",
}
KIND_ORDER = ("stop", "stop_near_side", "signal", "crossing")
OTHER_COLOR = "#888888"
CONG_COLOR = "#8e44ad"  # purple — per-segment residual congestion
