"""Generate all slide images for the 5-minute presentation.

Produces 14 PNGs in `slides/`:
  A1_archive.png         — multi-agency R2 archive (4 city heatmaps)
  A2_pings_only.png      — trip 1001350 raw pings on a map
  A3_pings_with_shape.png — same + GTFS shape underlay
  A4_ts_raw.png          — TS diagram, raw pings only, miles 3-6
  A5_ts_join.png         — same TS + join-the-dots
  A6_ts_locreg.png       — same TS + LOCREG-PCHIP bw=5
  A7_ts_smooth_only.png  — same TS, smooth only
  A8_ts_with_stops.png   — same TS + bus stop horizontal dotted lines
  A9_ts_with_intersections.png  — same + intersection horizontal dotted lines
  A10_mapmatch.png        — Valhalla map-matching explainer
  C5_multitrip.png        — 11 trip stringlines on the same TS diagram (full route)
  B1_speed.png            — trip 1001350 speed profile (miles 3-6) with vertical lines for every stop+intersection
  D2_pipeline.png         — pipeline diagram (boxes + arrows)
  E1_map.png — Route 22 SB intersections rendered on a basemap

Run:
    PYTHONPATH=src .venv/bin/python scripts/build_slides.py
"""

from __future__ import annotations

import io
import json
import math
from pathlib import Path

import contextily as cx
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np
import pandas as pd
import pyarrow.parquet as pq
from matplotlib.collections import LineCollection
from scipy.interpolate import CubicHermiteSpline

from dataio.intersections import load_intersections
from dataio.gtfs import load_gtfs_shape_with_dist, load_route_stops
from dataio.realtime import ARCHIVE_URL, fetch as _curl_fetch
from dataio.way_match import decode_polyline6

# --- shared constants -----------------------------------------------------
SHAPE_ID = "67803936"
TRIP_ID = "1001350_4017_2026-05-05"  # disambiguated _vehicle_id_chicago-date
M_PER_MI = 1609.344
WINDOW_MI = (6.0, 8.0)
SLIDE_DPI = 160
FIG_WIDESCREEN = (16, 9)         # standard slide aspect
FIG_TS = (12, 8)                 # squarer figure for TS + speed slides
FIG_SQUARE = (10, 10)
TS_FACE = "#fafbfc"

OUT = Path("figures")
OUT.mkdir(parents=True, exist_ok=True)

# CARTO Positron tile provider (same one our Leaflet maps use).
CARTO_LIGHT = cx.providers.CartoDB.PositronNoLabels
CARTO_LABELS = cx.providers.CartoDB.PositronOnlyLabels

# Route 22 SB color palette
PING_COLOR = "#1f4e79"
PING_RAW = "#3f6699"
LINE_BUS = "#cc4125"
LINE_BG = "#888"
SIGNAL_COLOR = "#dc8c32"
STOP_COLOR = "#cc0000"
STOP_BUS_COLOR = "#3a85d6"


# --- Small helpers --------------------------------------------------------


def latlon_to_webmercator(lats: np.ndarray, lons: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Convert WGS84 to Web Mercator (EPSG:3857) so it lines up with cx tiles."""
    R = 6378137.0
    x = R * np.radians(np.asarray(lons, dtype=float))
    y = R * np.log(np.tan(np.pi / 4 + np.radians(np.asarray(lats, dtype=float)) / 2))
    return x, y


def add_basemap(ax, source=CARTO_LIGHT):
    cx.add_basemap(ax, source=source, crs="EPSG:3857", attribution_size=6)


def trip_smoothed(trip_id: str, bw_dir: str = "outputs/out_r2_bw5") -> CubicHermiteSpline:
    """Rebuild a trip's PCHIP from its serialized record."""
    data = json.load(open(f"{bw_dir}/trajectories.json"))
    rec = next(t for t in data["trips"] if t["trip_id"] == trip_id)
    t = np.asarray(rec["t_knots"], dtype=float)
    x = np.asarray(rec["x_knots"], dtype=float)
    m = np.asarray(rec["slopes"], dtype=float)
    return CubicHermiteSpline(t, x, m, extrapolate=False)


def trip_raw(trip_id: str) -> pd.DataFrame:
    df = pd.read_csv("data/r2_route22_sb_all.csv", dtype=str)
    g = df[df.trip_id == trip_id].copy()
    g["latitude"] = g.latitude.astype(float)
    g["longitude"] = g.longitude.astype(float)
    g["avl_event_time"] = pd.to_datetime(g.avl_event_time, format="%Y-%m-%d %H:%M:%S.%f")
    g = g.drop_duplicates(["trip_id", "avl_event_time"]).sort_values("avl_event_time").reset_index(drop=True)
    return g


def style_ts(ax, window_mi=WINDOW_MI, t_seconds=None):
    ax.set_facecolor(TS_FACE)
    ax.set_xlabel("Time of day", fontsize=12)
    ax.set_ylabel("Distance along route (mi)", fontsize=12)
    ax.set_ylim(window_mi[0] - 0.05, window_mi[1] + 0.05)
    ax.grid(True, alpha=0.3, linewidth=0.5)
    ax.invert_yaxis()  # convention: route forward = y up; but for SB we want top=north
    # Actually keep normal — let's not invert; route progress on Y-axis is intuitive going up.
    ax.invert_yaxis()
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)


# --- Image generators -----------------------------------------------------


def slide_01_archive():
    """Multi-agency R2 archive: 4 small heatmaps (CTA + MBTA + MTA + TFL)."""
    print("[01] R2 archive heatmaps…")

    # We have CTA cached locally. For MBTA / MTA / TFL we'll pull a recent
    # hour from R2 if not already cached.
    pub = ARCHIVE_URL
    cache = Path("caches/realtime_archive")
    cache.mkdir(exist_ok=True)
    manifest_local = cache / "_manifest.parquet"
    _curl_fetch(f"{pub}/_manifest.parquet", manifest_local)
    manifest = pq.ParquetFile(manifest_local).read().to_pandas()

    agencies = ["cta", "mbta", "mta", "tfl"]
    centres = {                      # default city centres for the small map crops
        "cta":  (41.882, -87.629),   # Chicago
        "mbta": (42.358, -71.060),   # Boston
        "mta":  (40.752, -73.992),   # NYC (Manhattan)
        "tfl":  (51.510, -0.118),    # London
    }
    radii_km = {"cta": 25, "mbta": 18, "mta": 15, "tfl": 22}

    panels = {}
    for ag in agencies:
        ag_rows = manifest[manifest.agency == ag].sort_values(
            ["year", "month", "day", "hour"], ascending=False
        )
        if ag_rows.empty:
            continue
        latest = ag_rows.iloc[0]
        local = cache / latest.path.replace("/", "__")
        if not local.exists():
            print(f"  ↓ {ag} {latest.path}")
            _curl_fetch(f"{pub}/{latest.path}", local)
        df = pq.ParquetFile(local).read().to_pandas()
        df = df.dropna(subset=["latitude", "longitude"])
        # Keep a manageable sample for plotting density
        if len(df) > 80_000:
            df = df.sample(80_000, random_state=0)
        panels[ag] = df

    fig, axes = plt.subplots(2, 2, figsize=FIG_WIDESCREEN, dpi=SLIDE_DPI)
    titles = {"cta": "CTA — Chicago", "mbta": "MBTA — Boston",
              "mta": "MTA — NYC", "tfl": "TfL — London"}
    for ax, ag in zip(axes.ravel(), agencies):
        ax.set_facecolor("#f5f5f5")
        if ag not in panels:
            ax.text(0.5, 0.5, f"{ag.upper()}: no data", ha="center", va="center",
                    transform=ax.transAxes)
            ax.set_xticks([]); ax.set_yticks([])
            continue
        df = panels[ag]
        x, y = latlon_to_webmercator(df.latitude.values, df.longitude.values)
        # Crop to a reasonable urban-area extent around the city centre
        cy_lat, cy_lon = centres[ag]
        cx_, cy_ = latlon_to_webmercator(np.array([cy_lat]), np.array([cy_lon]))
        r_m = radii_km[ag] * 1000.0
        # Hex-bin density
        ax.hexbin(x, y, gridsize=80, cmap="magma_r", mincnt=1, alpha=0.85, linewidths=0)
        ax.set_xlim(cx_[0] - r_m, cx_[0] + r_m)
        ax.set_ylim(cy_[0] - r_m, cy_[0] + r_m)
        try:
            add_basemap(ax)
        except Exception as e:
            print(f"  basemap error for {ag}: {e}")
        ax.set_xticks([]); ax.set_yticks([])
        ax.set_title(f"{titles[ag]}: {len(df):,} pings",
                     fontsize=11, pad=4)

    fig.suptitle("Bus position archive — 1 hour of pings, 4 agencies",
                 fontsize=14, fontweight="bold")
    fig.tight_layout()
    out = OUT / "A1_archive.png"
    fig.savefig(out, dpi=SLIDE_DPI, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  saved: {out}")


def _trip_map(ax, with_shape: bool, ping_color: str = PING_COLOR):
    """Helper for slides 02 + 03."""
    raw = trip_raw(TRIP_ID)
    poly, _ = load_gtfs_shape_with_dist("data/gtfs/cta_gtfs.zip", SHAPE_ID)
    if with_shape:
        sx, sy = latlon_to_webmercator(poly[:, 0], poly[:, 1])
        ax.plot(sx, sy, color=LINE_BG, linewidth=4.0, alpha=0.85, solid_capstyle="round",
                label="GTFS shape (route)")
    px, py = latlon_to_webmercator(raw.latitude.values, raw.longitude.values)
    ax.scatter(px, py, s=22, color=ping_color, edgecolor="white", linewidth=0.5,
               zorder=4, label=f"raw AVL pings ({len(raw)})")
    # Crop tightly around route
    sx, sy = latlon_to_webmercator(poly[:, 0], poly[:, 1])
    pad = 800
    ax.set_xlim(sx.min() - pad, sx.max() + pad)
    ax.set_ylim(sy.min() - pad, sy.max() + pad)
    add_basemap(ax)
    ax.set_xticks([]); ax.set_yticks([])
    ax.legend(loc="upper right", fontsize=10, frameon=True)


def slide_02_pings_only():
    print("[02] trip 1001350 raw pings on map…")
    fig, ax = plt.subplots(figsize=(8, 14), dpi=SLIDE_DPI)
    _trip_map(ax, with_shape=False, ping_color="#EA3833")
    ax.set_title(f"Trip {TRIP_ID} — raw AVL pings on Route 22 SB",
                 fontsize=12, pad=8)
    fig.tight_layout()
    out = OUT / "A2_pings_only.png"
    fig.savefig(out, dpi=SLIDE_DPI, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  saved: {out}")


def slide_03_pings_with_shape():
    print("[03] trip 1001350 pings + GTFS shape…")
    fig, ax = plt.subplots(figsize=(8, 14), dpi=SLIDE_DPI)
    _trip_map(ax, with_shape=True)
    ax.set_title("Pings projected onto GTFS shape → 'distance along route'",
                 fontsize=12, pad=8)
    fig.tight_layout()
    out = OUT / "A3_pings_with_shape.png"
    fig.savefig(out, dpi=SLIDE_DPI, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  saved: {out}")


# --- TS diagrams (#04–#09) ------------------------------------------------


def _trip_window_data():
    """Return the data needed for the TS diagrams (raw pings + smoothed trajectory)
    pre-cropped to the WINDOW_MI window."""
    raw_full = pd.read_csv(f"out_r2_bw5/trip_{TRIP_ID}.csv")
    f = trip_smoothed(TRIP_ID)

    # Pull the raw lat/lon-derived d_raw for plotting (we already have d_raw_m).
    raw = raw_full.copy()
    raw["d_mi"] = raw.d_raw_m / M_PER_MI
    # The trip's CSV t_s starts at first ping; convert to absolute datetime.
    first_ts = pd.Timestamp(json.load(open("out_r2_bw5/trajectories.json"))["trips"][0]["first_ping_iso"])
    rec = next(t for t in json.load(open("out_r2_bw5/trajectories.json"))["trips"] if t["trip_id"] == TRIP_ID)
    first_ts = pd.Timestamp(rec["first_ping_iso"])
    raw["clock"] = first_ts + pd.to_timedelta(raw.t_s, unit="s")
    return raw, f, first_ts


def _smooth_grid(f, t_min: float, t_max: float, n: int = 1500):
    ts = np.linspace(t_min, t_max, n)
    xs = f(ts) / M_PER_MI
    return ts, xs


def _ts_window_axes(fig=None, ax=None, raw_in_window=None, smooth_in_window=None):
    if ax is None:
        fig, ax = plt.subplots(figsize=FIG_TS, dpi=SLIDE_DPI)
    style_ts(ax)
    return fig, ax


def _setup_ts_window():
    raw, f, first_ts = _trip_window_data()
    in_win_raw = raw[(raw.d_mi >= WINDOW_MI[0]) & (raw.d_mi <= WINDOW_MI[1])].copy()
    if in_win_raw.empty:
        raise RuntimeError("trip 1001350 has no raw pings in the chosen window")
    t0 = in_win_raw.t_s.min() - 30
    t1 = in_win_raw.t_s.max() + 30
    ts_grid, xs_grid = _smooth_grid(f, t0, t1)
    in_win_smooth = ((xs_grid >= WINDOW_MI[0]) & (xs_grid <= WINDOW_MI[1]))
    ts_grid = ts_grid[in_win_smooth]
    xs_grid = xs_grid[in_win_smooth]
    raw_clock = first_ts + pd.to_timedelta(in_win_raw.t_s.values, unit="s")
    smooth_clock = first_ts + pd.to_timedelta(ts_grid, unit="s")
    return raw_clock, in_win_raw, smooth_clock, xs_grid, first_ts


def _ts_xlim(raw_clock, smooth_clock):
    """A consistent x-range across slides 4-9, padded slightly."""
    pad = pd.Timedelta(seconds=60)
    lo = min(raw_clock.min(), smooth_clock.min()) - pad
    hi = max(raw_clock.max(), smooth_clock.max()) + pad
    return lo, hi


def _save_ts(fig, name: str):
    fig.tight_layout()
    out = OUT / name
    fig.savefig(out, dpi=SLIDE_DPI, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  saved: {out}")


def _ts_skeleton(title: str, raw_clock, smooth_clock):
    fig, ax = plt.subplots(figsize=FIG_TS, dpi=SLIDE_DPI)
    style_ts(ax)
    ax.set_xlim(*_ts_xlim(raw_clock, smooth_clock))
    ax.set_title(title, fontsize=13, pad=8)
    return fig, ax


def slide_04_ts_raw():
    print("[04] TS raw pings only…")
    rc, raw_win, sc, xs, t0 = _setup_ts_window()
    fig, ax = _ts_skeleton(
        f"Trip {TRIP_ID} — raw AVL pings (Route 22 SB, miles {WINDOW_MI[0]:g}–{WINDOW_MI[1]:g})",
        rc, sc,
    )
    ax.scatter(rc, raw_win.d_mi, s=30, color=PING_RAW,
               edgecolor="white", linewidth=0.5, zorder=3, label="raw pings")
    ax.legend(loc="lower right", frameon=True)
    _save_ts(fig, "A4_ts_raw.png")


def slide_05_ts_join():
    print("[05] TS join-the-dots…")
    rc, raw_win, sc, xs, t0 = _setup_ts_window()
    fig, ax = _ts_skeleton(f"Trip {TRIP_ID} — join-the-dots stringline",
                            rc, sc)
    ax.plot(rc, raw_win.d_mi, color="#666", linewidth=1.4, zorder=2, label="join-the-dots")
    ax.scatter(rc, raw_win.d_mi, s=24, color=PING_RAW,
               edgecolor="white", linewidth=0.5, zorder=3)
    ax.legend(loc="lower right", frameon=True)
    _save_ts(fig, "A5_ts_join.png")


def slide_06_ts_locreg():
    print("[06] TS LOCREG-PCHIP smooth + raw…")
    rc, raw_win, sc, xs, t0 = _setup_ts_window()
    fig, ax = _ts_skeleton(f"Trip {TRIP_ID} — LOCREG-PCHIP (bw=5)", rc, sc)
    ax.plot(sc, xs, color=LINE_BUS, linewidth=2.4, zorder=4, label="LOCREG-PCHIP bw=5")
    ax.scatter(rc, raw_win.d_mi, s=22, color=PING_RAW, alpha=0.7,
               edgecolor="white", linewidth=0.5, zorder=3, label="raw pings")
    ax.legend(loc="lower right", frameon=True)
    _save_ts(fig, "A6_ts_locreg.png")


def slide_07_ts_smooth_only():
    print("[07] TS smooth only…")
    rc, raw_win, sc, xs, t0 = _setup_ts_window()
    fig, ax = _ts_skeleton(f"Trip {TRIP_ID} — smoothed trajectory", rc, sc)
    ax.plot(sc, xs, color=LINE_BUS, linewidth=2.6, zorder=4)
    _save_ts(fig, "A7_ts_smooth_only.png")


def _draw_horizontal_lines_with_labels(ax, items, *, color, linestyle, label_xpos=0.005):
    """Draw horizontal dotted lines at given y-values (mi) with right-aligned labels."""
    for it in items:
        y = it["dist_mi"]
        ax.axhline(y, color=color, linewidth=0.6, linestyle=linestyle, alpha=0.7, zorder=1)
        ax.text(label_xpos, y, it["name"], transform=ax.get_yaxis_transform(),
                fontsize=7, color="#444", ha="right", va="center",
                bbox=dict(facecolor="white", edgecolor="none", pad=0.5, alpha=0.85))


def _stops_in_window():
    stops = load_route_stops("data/gtfs/cta_gtfs.zip", SHAPE_ID)
    return [
        {"name": s["name"], "dist_mi": s["dist_along_m"] / M_PER_MI}
        for s in stops
        if WINDOW_MI[0] <= s["dist_along_m"] / M_PER_MI <= WINDOW_MI[1]
    ]


def _intersections_in_window():
    ints = load_intersections("intersections_route22.json")[SHAPE_ID]
    out = []
    for cp in ints:
        d_mi = cp.dist_along_route_m / M_PER_MI
        if not (WINDOW_MI[0] <= d_mi <= WINDOW_MI[1]):
            continue
        cross = " / ".join(cp.cross_street_names) if cp.cross_street_names else "(unnamed)"
        out.append({"name": cross, "dist_mi": d_mi, "type": cp.control_type})
    return out


def slide_08_ts_with_stops():
    print("[08] TS smooth + bus stops…")
    rc, raw_win, sc, xs, t0 = _setup_ts_window()
    fig, ax = _ts_skeleton(f"Trip {TRIP_ID} + bus stops", rc, sc)
    stops = _stops_in_window()
    # Move axis area right to make room for left-side labels
    fig.subplots_adjust(left=0.18, right=0.97, top=0.92, bottom=0.10)
    _draw_horizontal_lines_with_labels(ax, stops, color=STOP_BUS_COLOR, linestyle=":")
    ax.plot(sc, xs, color=LINE_BUS, linewidth=2.6, zorder=4, label="bus trajectory")
    ax.legend(loc="lower right", frameon=True)
    _save_ts(fig, "A8_ts_with_stops.png")


def slide_09_ts_with_intersections():
    print("[09] TS smooth + stops + intersections…")
    rc, raw_win, sc, xs, t0 = _setup_ts_window()
    fig, ax = _ts_skeleton(f"Trip {TRIP_ID} + bus stops + controlled intersections",
                            rc, sc)
    fig.subplots_adjust(left=0.22, right=0.97, top=0.92, bottom=0.10)
    stops = _stops_in_window()
    inters = _intersections_in_window()
    _draw_horizontal_lines_with_labels(ax, stops, color=STOP_BUS_COLOR, linestyle=":")
    # Draw each intersection in its color
    for it in inters:
        c = SIGNAL_COLOR if it["type"] == "traffic_signals" else STOP_COLOR
        ax.axhline(it["dist_mi"], color=c, linewidth=0.7, linestyle="--",
                   alpha=0.7, zorder=2)
    ax.plot(sc, xs, color=LINE_BUS, linewidth=2.6, zorder=5, label="bus trajectory")

    # Custom legend with three layers
    legend_handles = [
        mpatches.Patch(color=STOP_BUS_COLOR, label="bus stops (dotted)"),
        mpatches.Patch(color=SIGNAL_COLOR, label="traffic signals (dashed)"),
        mpatches.Patch(color=STOP_COLOR, label="stop signs (dashed)"),
    ]
    ax.legend(handles=legend_handles, loc="lower right", frameon=True, fontsize=9)
    _save_ts(fig, "A9_ts_with_intersections.png")


# --- Map matching explainer (#10) ----------------------------------------


def slide_10_mapmatch():
    """Show GTFS shape + numbered raw pings + Valhalla matched route on a basemap.

    Picks a small (~2-block) section of Route 22 SB with an interesting
    intersection so the matching is visible. We use the existing way_match
    cache: each WaySegment is one OSM-way piece of the matched route. We
    color those segments to make the matched edge sequence visible vs. the
    GTFS shape.
    """
    print("[10] map-matching explainer…")
    from dataio.way_match import call_valhalla, load_cache as load_way_cache
    poly, dist = load_gtfs_shape_with_dist("data/gtfs/cta_gtfs.zip", SHAPE_ID)

    # Choose a sub-window of the GTFS shape — about 250 m around an
    # intersection. Pick mile 5.95 (Belmont) as the focus.
    target_mi = 5.95
    half_m = 200.0
    lo, hi = target_mi * M_PER_MI - half_m, target_mi * M_PER_MI + half_m
    keep = (dist >= lo) & (dist <= hi)
    sub_poly = poly[keep]

    # Call Valhalla on this sub-polyline so we can render the matched edges.
    resp = call_valhalla(sub_poly, endpoint="http://localhost:8002")
    matched_shape = decode_polyline6(resp.get("shape", ""))
    edges = resp.get("edges", [])
    matched_pts = resp.get("matched_points", [])

    # Use the same trip's pings within this distance window (the pings are
    # close to the GTFS shape and demonstrate "noisy GPS → matched route").
    raw = trip_raw(TRIP_ID)
    raw["d_mi"] = pd.read_csv(f"out_r2_bw5/trip_{TRIP_ID}.csv").d_raw_m.values / M_PER_MI
    sub_raw = raw[(raw.d_mi >= target_mi - half_m / M_PER_MI) &
                   (raw.d_mi <= target_mi + half_m / M_PER_MI)].reset_index(drop=True)

    fig, ax = plt.subplots(figsize=FIG_WIDESCREEN, dpi=SLIDE_DPI)
    # 1. GTFS shape (gray)
    sx, sy = latlon_to_webmercator(sub_poly[:, 0], sub_poly[:, 1])
    ax.plot(sx, sy, color=LINE_BG, linewidth=6, alpha=0.5, zorder=2,
            solid_capstyle="round", label="GTFS shape (input)")

    # 2. Matched route — color each edge a different color
    if matched_shape and edges:
        ms = np.array(matched_shape)
        mx, my = latlon_to_webmercator(ms[:, 0], ms[:, 1])
        # Group shape vertices by edge_index
        for i, e in enumerate(edges):
            bi = e.get("begin_shape_index"); ei = e.get("end_shape_index")
            if bi is None or ei is None or bi == ei:
                continue
            color = plt.cm.tab10(i % 10)
            ax.plot(mx[bi:ei + 1], my[bi:ei + 1], color=color, linewidth=4,
                    alpha=0.9, zorder=3,
                    label=f"way {e.get('way_id', '?')}" if i < 3 else None)

    # 3. Raw pings — numbered circles, like the Valhalla example image
    if not sub_raw.empty:
        rx, ry = latlon_to_webmercator(sub_raw.latitude.values, sub_raw.longitude.values)
        ax.scatter(rx, ry, s=80, color=PING_COLOR, edgecolor="white", linewidth=1.2,
                   zorder=5)
        for i, (x, y) in enumerate(zip(rx, ry)):
            ax.annotate(str(i), (x, y), color="white", fontsize=8,
                        ha="center", va="center", zorder=6, fontweight="bold")
        # Add light blue lines from each ping to its snapped position
        if matched_pts:
            for i, m in enumerate(matched_pts):
                if i >= len(rx):
                    break
                snap_lat = m.get("lat"); snap_lon = m.get("lon")
                if snap_lat is None or snap_lon is None:
                    continue
                sxp, syp = latlon_to_webmercator(np.array([snap_lat]), np.array([snap_lon]))
                ax.plot([rx[i], sxp[0]], [ry[i], syp[0]], color="#aaccee",
                        linewidth=1.0, alpha=0.7, zorder=4)

    # Crop tightly
    pad = 100
    ax.set_xlim(sx.min() - pad, sx.max() + pad)
    ax.set_ylim(sy.min() - pad, sy.max() + pad)
    add_basemap(ax)
    ax.set_xticks([]); ax.set_yticks([])
    ax.set_title("Map-matching: GTFS shape → Valhalla → OSM way sequence  "
                 "(blue = noisy ping, faint blue = snap, colors = matched edges)",
                 fontsize=11, pad=8)
    ax.legend(loc="upper left", fontsize=9, framealpha=0.9)
    fig.tight_layout()
    out = OUT / "A10_mapmatch.png"
    fig.savefig(out, dpi=SLIDE_DPI, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  saved: {out}")


# --- Additional images (A, B, C, E) --------------------------------------


def slide_A_multitrip():
    """Stringlines for all 11 trips on one TS diagram, full route."""
    print("[A] multi-trip overlay…")
    data = json.load(open("out_r2_bw5/trajectories.json"))["trips"]
    stops = load_route_stops("data/gtfs/cta_gtfs.zip", SHAPE_ID)

    fig, ax = plt.subplots(figsize=FIG_WIDESCREEN, dpi=SLIDE_DPI)
    style_ts(ax, window_mi=(0, 11))
    ax.set_ylim(0, 11)
    ax.set_xlabel("Time of day", fontsize=12)
    ax.set_title("All 11 R2 Route 22 SB trip stringlines on one frame",
                 fontsize=13, pad=8)

    # Bus stop horizontal dotted lines
    for s in stops:
        ax.axhline(s["dist_along_m"] / M_PER_MI, color="#bbb", linewidth=0.4,
                   linestyle=":", alpha=0.5, zorder=1)

    palette = plt.cm.tab10.colors
    for i, rec in enumerate(data):
        f = trip_smoothed(rec["trip_id"])
        first_ts = pd.Timestamp(rec["first_ping_iso"])
        ts = np.linspace(rec["t_knots"][0], rec["t_knots"][-1], 1500)
        xs = f(ts) / M_PER_MI
        clock = first_ts + pd.to_timedelta(ts, unit="s")
        c = palette[i % len(palette)]
        ax.plot(clock, xs, color=c, linewidth=1.4, alpha=0.85, zorder=3,
                label=f"{rec['trip_id']} (bus {rec['bus_id']})")

    ax.legend(loc="lower right", fontsize=8, ncol=2, framealpha=0.9)
    ax.grid(True, alpha=0.3, linewidth=0.5)
    fig.autofmt_xdate()
    fig.tight_layout()
    out = OUT / "C5_multitrip.png"
    fig.savefig(out, dpi=SLIDE_DPI, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  saved: {out}")


def slide_B_speed():
    """Trip 1001350 speed profile, miles 3-6, with vertical lines for stops + intersections."""
    print("[B] speed profile (miles 3-6) with stop+control vertical lines…")
    f = trip_smoothed(TRIP_ID)
    ts = np.linspace(f.x[0], f.x[-1], 4000)
    d_mi = f(ts) / M_PER_MI
    v_mph = f.derivative()(ts) * 2.23694
    in_win = (d_mi >= WINDOW_MI[0]) & (d_mi <= WINDOW_MI[1])
    d_w = d_mi[in_win]; v_w = v_mph[in_win]

    fig, ax = plt.subplots(figsize=FIG_TS, dpi=SLIDE_DPI)
    ax.set_facecolor(TS_FACE)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)

    # Vertical lines for stops + controls
    for s in _stops_in_window():
        ax.axvline(s["dist_mi"], color=STOP_BUS_COLOR, linewidth=0.5,
                   linestyle=":", alpha=0.6, zorder=1)
    for it in _intersections_in_window():
        c = SIGNAL_COLOR if it["type"] == "traffic_signals" else STOP_COLOR
        ax.axvline(it["dist_mi"], color=c, linewidth=0.7, linestyle="--",
                   alpha=0.7, zorder=2)

    # Speed line
    ax.plot(d_w, v_w, color=LINE_BUS, linewidth=2.0, zorder=4)

    ax.set_xlim(*WINDOW_MI)
    ax.set_xlabel("Distance along route (mi)", fontsize=12)
    ax.set_ylabel("Speed (mph)", fontsize=12)
    ax.set_ylim(bottom=0)
    ax.grid(True, alpha=0.3, linewidth=0.5)
    ax.set_title(f"Trip {TRIP_ID} — speed profile (miles {WINDOW_MI[0]}–{WINDOW_MI[1]})\n"
                 "vertical lines: blue dotted = bus stops, "
                 "amber dashed = signals, red dashed = stop signs",
                 fontsize=11, pad=8)
    fig.tight_layout()
    out = OUT / "B1_speed.png"
    fig.savefig(out, dpi=SLIDE_DPI, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  saved: {out}")


def _speed_window():
    f = trip_smoothed(TRIP_ID)
    ts = np.linspace(f.x[0], f.x[-1], 4000)
    d_mi = f(ts) / M_PER_MI
    v_mph = f.derivative()(ts) * 2.23694
    in_win = (d_mi >= WINDOW_MI[0]) & (d_mi <= WINDOW_MI[1])
    return d_mi[in_win], v_mph[in_win]


def _speed_axes():
    fig, ax = plt.subplots(figsize=FIG_TS, dpi=SLIDE_DPI)
    ax.set_facecolor("white")
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.set_xlabel("Distance along route (mi)", fontsize=12)
    ax.set_ylabel("Speed (mph)", fontsize=12)
    ax.grid(False)
    return fig, ax


def _finalize_speed(ax):
    ax.set_xlim(*WINDOW_MI)
    ax.set_ylim(bottom=0)


def slide_B1_speed_plain():
    """Speed profile, no vertical lines, no grid."""
    print("[B1] speed profile, no vertical lines…")
    d_w, v_w = _speed_window()
    fig, ax = _speed_axes()
    ax.plot(d_w, v_w, color=LINE_BUS, linewidth=2.2, zorder=4)
    _finalize_speed(ax)
    ax.set_title(f"Trip {TRIP_ID} — speed profile (miles {WINDOW_MI[0]}–{WINDOW_MI[1]})",
                 fontsize=12, pad=8)
    fig.tight_layout()
    out = OUT / "B2_speed.png"
    fig.savefig(out, dpi=SLIDE_DPI, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  saved: {out}")


def slide_B2_speed_with_stops():
    """Speed profile with bus stops only — thicker, more visible blue lines."""
    print("[B2] speed profile + bus stops…")
    d_w, v_w = _speed_window()
    fig, ax = _speed_axes()
    for s in _stops_in_window():
        ax.axvline(s["dist_mi"], color=STOP_BUS_COLOR, linewidth=1.6,
                   linestyle="-", alpha=0.55, zorder=1)
    ax.plot(d_w, v_w, color=LINE_BUS, linewidth=2.2, zorder=4)
    _finalize_speed(ax)
    ax.set_title(f"Trip {TRIP_ID} — speed profile + bus stops "
                 f"(miles {WINDOW_MI[0]}–{WINDOW_MI[1]})",
                 fontsize=12, pad=8)
    legend_handles = [
        mpatches.Patch(color=STOP_BUS_COLOR, label="bus stops"),
        mpatches.Patch(color=LINE_BUS, label="speed"),
    ]
    ax.legend(handles=legend_handles, loc="upper right", frameon=True, fontsize=9)
    fig.tight_layout()
    out = OUT / "B3_speed.png"
    fig.savefig(out, dpi=SLIDE_DPI, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  saved: {out}")


def slide_B3_speed_with_stops_and_signals():
    """Speed profile with bus stops + traffic signals (no stop signs)."""
    print("[B3] speed profile + bus stops + traffic signals…")
    d_w, v_w = _speed_window()
    fig, ax = _speed_axes()
    for s in _stops_in_window():
        ax.axvline(s["dist_mi"], color=STOP_BUS_COLOR, linewidth=1.6,
                   linestyle="-", alpha=0.55, zorder=1)
    for it in _intersections_in_window():
        if it["type"] != "traffic_signals":
            continue
        ax.axvline(it["dist_mi"], color=SIGNAL_COLOR, linewidth=1.4,
                   linestyle="--", alpha=0.85, zorder=2)
    ax.plot(d_w, v_w, color=LINE_BUS, linewidth=2.2, zorder=4)
    _finalize_speed(ax)
    ax.set_title(f"Trip {TRIP_ID} — speed profile + bus stops + traffic signals "
                 f"(miles {WINDOW_MI[0]}–{WINDOW_MI[1]})",
                 fontsize=12, pad=8)
    legend_handles = [
        mpatches.Patch(color=STOP_BUS_COLOR, label="bus stops"),
        mpatches.Patch(color=SIGNAL_COLOR, label="traffic signals"),
        mpatches.Patch(color=LINE_BUS, label="speed"),
    ]
    ax.legend(handles=legend_handles, loc="upper right", frameon=True, fontsize=9)
    fig.tight_layout()
    out = OUT / "B4_speed.png"
    fig.savefig(out, dpi=SLIDE_DPI, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  saved: {out}")


def slide_B4_speed_with_delay_shading():
    """Same as B3, plus red vertical shading wherever v < 5 mph."""
    print("[B4] speed profile + bus stops + traffic signals + v<5mph shading…")
    d_w, v_w = _speed_window()
    fig, ax = _speed_axes()
    for s in _stops_in_window():
        ax.axvline(s["dist_mi"], color=STOP_BUS_COLOR, linewidth=1.6,
                   linestyle="-", alpha=0.55, zorder=1)
    for it in _intersections_in_window():
        if it["type"] != "traffic_signals":
            continue
        ax.axvline(it["dist_mi"], color=SIGNAL_COLOR, linewidth=1.4,
                   linestyle="--", alpha=0.85, zorder=2)

    slow = v_w < 5.0
    if slow.any():
        edges = np.diff(slow.astype(np.int8))
        starts = np.where(edges == 1)[0] + 1
        ends = np.where(edges == -1)[0] + 1
        if slow[0]:
            starts = np.concatenate(([0], starts))
        if slow[-1]:
            ends = np.concatenate((ends, [len(slow)]))
        for s, e in zip(starts, ends):
            ax.axvspan(d_w[s], d_w[e - 1], color="#cc0000", alpha=0.15,
                       linewidth=0, zorder=0)

    ax.plot(d_w, v_w, color=LINE_BUS, linewidth=2.2, zorder=4)
    ax.axhline(5.0, color="#cc0000", linewidth=0.8, linestyle=":",
               alpha=0.6, zorder=3)
    _finalize_speed(ax)
    ax.set_title(f"Trip {TRIP_ID} — speed profile with delay segments (v < 5 mph) "
                 f"shaded\n(miles {WINDOW_MI[0]}–{WINDOW_MI[1]})",
                 fontsize=12, pad=8)
    legend_handles = [
        mpatches.Patch(color=STOP_BUS_COLOR, label="bus stops"),
        mpatches.Patch(color=SIGNAL_COLOR, label="traffic signals"),
        mpatches.Patch(color="#cc0000", alpha=0.15, label="v < 5 mph"),
        mpatches.Patch(color=LINE_BUS, label="speed"),
    ]
    ax.legend(handles=legend_handles, loc="upper right", frameon=True, fontsize=9)
    fig.tight_layout()
    out = OUT / "B5_speed.png"
    fig.savefig(out, dpi=SLIDE_DPI, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  saved: {out}")


def slide_B5_speed_with_delay_shading_10mph():
    """Same as B4 but using v < 10 mph as the slow threshold."""
    print("[B5] speed profile + bus stops + traffic signals + v<10mph shading…")
    d_w, v_w = _speed_window()
    fig, ax = _speed_axes()
    for s in _stops_in_window():
        ax.axvline(s["dist_mi"], color=STOP_BUS_COLOR, linewidth=1.6,
                   linestyle="-", alpha=0.55, zorder=1)
    for it in _intersections_in_window():
        if it["type"] != "traffic_signals":
            continue
        ax.axvline(it["dist_mi"], color=SIGNAL_COLOR, linewidth=1.4,
                   linestyle="--", alpha=0.85, zorder=2)

    slow = v_w < 10.0
    if slow.any():
        edges = np.diff(slow.astype(np.int8))
        starts = np.where(edges == 1)[0] + 1
        ends = np.where(edges == -1)[0] + 1
        if slow[0]:
            starts = np.concatenate(([0], starts))
        if slow[-1]:
            ends = np.concatenate((ends, [len(slow)]))
        for s, e in zip(starts, ends):
            ax.axvspan(d_w[s], d_w[e - 1], color="#cc0000", alpha=0.15,
                       linewidth=0, zorder=0)

    ax.plot(d_w, v_w, color=LINE_BUS, linewidth=2.2, zorder=4)
    ax.axhline(10.0, color="#cc0000", linewidth=0.8, linestyle=":",
               alpha=0.6, zorder=3)
    _finalize_speed(ax)
    ax.set_title(f"Trip {TRIP_ID} — speed profile with delay segments (v < 10 mph) "
                 f"shaded\n(miles {WINDOW_MI[0]}–{WINDOW_MI[1]})",
                 fontsize=12, pad=8)
    legend_handles = [
        mpatches.Patch(color=STOP_BUS_COLOR, label="bus stops"),
        mpatches.Patch(color=SIGNAL_COLOR, label="traffic signals"),
        mpatches.Patch(color="#cc0000", alpha=0.15, label="v < 10 mph"),
        mpatches.Patch(color=LINE_BUS, label="speed"),
    ]
    ax.legend(handles=legend_handles, loc="upper right", frameon=True, fontsize=9)
    fig.tight_layout()
    out = OUT / "B6_speed.png"
    fig.savefig(out, dpi=SLIDE_DPI, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  saved: {out}")


def slide_C_pipeline():
    """Pipeline diagram — boxes + arrows."""
    print("[C] pipeline diagram…")
    fig, ax = plt.subplots(figsize=FIG_WIDESCREEN, dpi=SLIDE_DPI)
    ax.set_xlim(0, 16); ax.set_ylim(0, 9)
    ax.axis("off")

    boxes = [
        # (x, y, w, h, label, sub, color)
        (0.6, 5.5, 2.6, 1.6, "AVL pings\n(R2 archive)",
         "lat, lon, time", "#cfe2f3"),
        (3.8, 5.5, 2.6, 1.6, "GTFS shape\n+ shape_dist_traveled",
         "polyline + meters per vertex", "#cfe2f3"),
        (7.0, 5.5, 2.6, 1.6, "Snap to shape",
         "(lat, lon) → dist_along_m", "#d9ead3"),
        (10.2, 5.5, 2.6, 1.6, "LOCREG-PCHIP",
         "smooth, monotone trajectory", "#d9ead3"),
        (13.4, 5.5, 2.0, 1.6, "Trajectory\nf(t)",
         "speed = f'(t)", "#fff2cc"),

        (3.8, 2.5, 2.6, 1.6, "Valhalla\ntrace_attributes",
         "GTFS shape → OSM ways", "#fce5cd"),
        (7.0, 2.5, 2.6, 1.6, "Way-match cache",
         "(way_id, dist_start, dist_end)", "#fce5cd"),
        (10.2, 2.5, 2.6, 1.6, "Overpass +\nintersections.py",
         "controlled intersections + stops", "#fce5cd"),
        (13.4, 2.5, 2.0, 1.6, "Control\npoints",
         "signals + stop signs", "#fff2cc"),

        (7.0, 0.2, 5.6, 1.4, "Delay attribution (next)",
         "join trajectory dwells with control points", "#f4cccc"),
    ]
    for (x, y, w, h, lbl, sub, fill) in boxes:
        rect = mpatches.FancyBboxPatch(
            (x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.12",
            facecolor=fill, edgecolor="#444", linewidth=1.2)
        ax.add_patch(rect)
        ax.text(x + w/2, y + h/2 + 0.18, lbl, ha="center", va="center",
                fontsize=11, fontweight="bold")
        ax.text(x + w/2, y + h/2 - 0.30, sub, ha="center", va="center",
                fontsize=8.5, style="italic", color="#444")

    # Arrows
    arrow_kw = dict(arrowstyle="->", color="#444", lw=1.6,
                     mutation_scale=18, capstyle="round")
    pairs = [
        ((3.2, 6.3), (3.8, 6.3)),
        ((6.4, 6.3), (7.0, 6.3)),
        ((9.6, 6.3), (10.2, 6.3)),
        ((12.8, 6.3), (13.4, 6.3)),
        ((5.1, 5.5), (5.1, 4.1)),     # GTFS to valhalla
        ((6.4, 3.3), (7.0, 3.3)),
        ((9.6, 3.3), (10.2, 3.3)),
        ((12.8, 3.3), (13.4, 3.3)),
        # Both flows feed the next stage
        ((14.4, 5.5), (12.0, 1.6)),
        ((14.4, 2.5), (12.0, 1.6)),
    ]
    for a, b in pairs:
        ax.annotate("", xy=b, xytext=a, arrowprops=arrow_kw)

    ax.text(8, 8.4, "Bus-trajectory reconstruction + delay-attribution pipeline",
            fontsize=14, fontweight="bold", ha="center")
    ax.text(8, 7.95, "(this presentation: through the trajectory + control-points stage; "
                       "delay attribution is upcoming)",
            fontsize=9, ha="center", color="#666", style="italic")
    fig.tight_layout()
    out = OUT / "D2_pipeline.png"
    fig.savefig(out, dpi=SLIDE_DPI, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  saved: {out}")


def slide_E_intersections_map():
    """Render intersections on a basemap (matplotlib equivalent of intersections_22sb.html)."""
    print("[E] intersections on basemap…")
    poly, _ = load_gtfs_shape_with_dist("data/gtfs/cta_gtfs.zip", SHAPE_ID)
    cps = load_intersections("intersections_route22.json")[SHAPE_ID]

    fig, ax = plt.subplots(figsize=(7, 14), dpi=SLIDE_DPI)
    sx, sy = latlon_to_webmercator(poly[:, 0], poly[:, 1])
    ax.plot(sx, sy, color="#666", linewidth=3, alpha=0.85, zorder=2,
            solid_capstyle="round", label="Route 22 SB shape")

    sig_lats = np.array([cp.lat for cp in cps if cp.control_type == "traffic_signals"])
    sig_lons = np.array([cp.lon for cp in cps if cp.control_type == "traffic_signals"])
    stp_lats = np.array([cp.lat for cp in cps if cp.control_type == "stop"])
    stp_lons = np.array([cp.lon for cp in cps if cp.control_type == "stop"])

    if len(sig_lats):
        sx_s, sy_s = latlon_to_webmercator(sig_lats, sig_lons)
        ax.scatter(sx_s, sy_s, s=80, marker="o", facecolor=SIGNAL_COLOR,
                   edgecolor="black", linewidth=0.9, zorder=4,
                   label=f"traffic signals ({len(sig_lats)})")
    if len(stp_lats):
        sx_t, sy_t = latlon_to_webmercator(stp_lats, stp_lons)
        ax.scatter(sx_t, sy_t, s=110, marker="8", facecolor=STOP_COLOR,
                   edgecolor="black", linewidth=0.9, zorder=4,
                   label=f"stop signs ({len(stp_lats)})")

    pad = 600
    ax.set_xlim(sx.min() - pad, sx.max() + pad)
    ax.set_ylim(sy.min() - pad, sy.max() + pad)
    add_basemap(ax)
    ax.set_xticks([]); ax.set_yticks([])
    ax.legend(loc="upper right", fontsize=10, frameon=True)
    ax.set_title(f"Route 22 SB — {len(cps)} controlled intersections from OSM",
                 fontsize=11, pad=8)
    fig.tight_layout()
    out = OUT / "E1_map.png"
    fig.savefig(out, dpi=SLIDE_DPI, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  saved: {out}")


# --- main ----------------------------------------------------------------


def main():
    slide_01_archive()
    slide_02_pings_only()
    slide_03_pings_with_shape()
    slide_04_ts_raw()
    slide_05_ts_join()
    slide_06_ts_locreg()
    slide_07_ts_smooth_only()
    slide_08_ts_with_stops()
    slide_09_ts_with_intersections()
    slide_10_mapmatch()
    slide_A_multitrip()
    slide_B_speed()
    slide_C_pipeline()
    slide_E_intersections_map()
    print(f"\nAll done. Output in {OUT}/")


if __name__ == "__main__":
    main()
