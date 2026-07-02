"""Multi-bandwidth comparison HTML viewer.

Reads multiple ``out_bw*`` runs (each with its own ``trajectories.json``) plus
the raw per-ping CSVs and assembles a single interactive HTML page where:

  * Trips, bandwidths, and raw-ping visibility are independently toggle-able.
  * The x-axis can be switched between absolute clock time and minutes since
    each trip's actual departure (last stationary ping before forward
    movement).
  * Raw GPS dots are rendered first (bottom Z-order) at 30% opacity when shown.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import plotly.io as pio
from scipy.interpolate import CubicHermiteSpline

from dataio.gtfs import load_route_stops, shape_id_for_pattern
from core.serialize import from_pchip_record, load_records

_M_PER_MI = 1609.344
_DEFAULT_DEPART_THRESHOLD_M = 0.03 * _M_PER_MI  # ~50m, matches earlier analysis
_BW_DIR_RE = re.compile(r"bw(\d+)$")


@dataclass(frozen=True)
class _TraceMeta:
    """Tags every plot trace so JS can find it for toggling."""

    trip: str
    bw: int | None  # None ⇒ raw scatter
    kind: str  # "smooth" or "raw"


def _bw_from_dirname(p: Path) -> int:
    m = _BW_DIR_RE.search(p.name)
    if not m:
        raise ValueError(f"directory name {p.name!r} doesn't match out_bw<N>")
    return int(m.group(1))


def _detect_departure_seconds(f: CubicHermiteSpline, threshold_m: float) -> float:
    """Smallest t since trip start where ``f(t) - f(t0) >= threshold_m``.

    Falls back to ``f.x[0]`` if the trip never crosses the threshold.
    """
    t0 = float(f.x[0])
    base = float(f(t0))
    grid = np.linspace(t0, float(f.x[-1]), 4000)
    vals = f(grid)
    above = np.where(vals - base >= threshold_m)[0]
    if above.size == 0:
        return t0
    return float(grid[above[0]])


def make_comparison_html(
    bw_dirs: list[Path],
    out_path: Path,
    raw_dir: Path | None = None,
    title: str = "Bandwidth comparison",
    n_eval: int = 1500,
    depart_threshold_m: float = _DEFAULT_DEPART_THRESHOLD_M,
    embed_js: bool = False,
    gtfs_zip_path: Path | None = None,
    pattern_id: str | None = None,
    chart_height_px: int = 1400,
    exclude_bus_ids: tuple[str, ...] = (),
    x_compress: float = 1.5,
) -> Path:
    """Build the comparison HTML.

    Parameters
    ----------
    bw_dirs
        Directories produced by ``--serialize``, e.g. ``[out_bw20, out_bw15,
        out_bw10, out_bw5]``. Bandwidth is parsed from each directory name.
    raw_dir
        Where to read raw per-ping CSVs from (``trip_<id>.csv``). Defaults to
        the first entry of ``bw_dirs`` (raw pings are bandwidth-independent).
    out_path
        Output HTML path.
    """
    if not bw_dirs:
        raise ValueError("need at least one bandwidth directory")
    if raw_dir is None:
        raw_dir = bw_dirs[0]

    # Sort bandwidths ascending so the smallest (most data-faithful) lands at
    # the top of the legend and is the default-checked / solid-line trace.
    bw_dirs = sorted(bw_dirs, key=_bw_from_dirname)
    exclude = set(exclude_bus_ids)

    # Load all trajectories per bandwidth.
    bws: list[int] = []
    traj_by_bw: dict[int, dict[str, dict]] = {}
    for d in bw_dirs:
        bw = _bw_from_dirname(d)
        bws.append(bw)
        records = load_records(d / "trajectories.json")
        traj_by_bw[bw] = {r["trip_id"]: r for r in records}

    # Trip union (in practice all bws share the same set), with bus-id filter.
    candidate_trips = {tid for recs in traj_by_bw.values() for tid in recs.keys()}
    trip_ids = sorted(
        [
            tid
            for tid in candidate_trips
            if all(traj_by_bw[bw].get(tid, {}).get("bus_id") not in exclude for bw in bws)
        ],
        key=lambda s: traj_by_bw[bws[0]][s]["first_ping_iso"]
        if s in traj_by_bw[bws[0]]
        else "",
    )

    # Compute departure time per trip (using the largest bw — smoothest f).
    largest_bw = max(bws)
    smallest_bw = min(bws)  # default-checked, solid line
    departure_s: dict[str, float] = {}
    first_ping_iso: dict[str, str] = {}
    bus_id: dict[str, str] = {}
    for tid in trip_ids:
        rec = traj_by_bw[largest_bw][tid]
        f = from_pchip_record(rec)
        departure_s[tid] = _detect_departure_seconds(f, depart_threshold_m)
        first_ping_iso[tid] = rec["first_ping_iso"]
        bus_id[tid] = rec["bus_id"]

    # Load raw pings per trip from raw_dir.
    raw_t_s: dict[str, np.ndarray] = {}
    raw_d_m: dict[str, np.ndarray] = {}
    for tid in trip_ids:
        csv = raw_dir / f"trip_{tid}.csv"
        if not csv.exists():
            continue
        df = pd.read_csv(csv, usecols=["t_s", "d_raw_m"])
        raw_t_s[tid] = df["t_s"].to_numpy()
        raw_d_m[tid] = df["d_raw_m"].to_numpy()

    # Color palette for trips (8+ colors).
    palette = [
        "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd",
        "#8c564b", "#e377c2", "#7f7f7f", "#bcbd22", "#17becf",
    ]
    trip_color: dict[str, str] = {tid: palette[i % len(palette)] for i, tid in enumerate(trip_ids)}

    # Bandwidth styling: smallest bw (default) = solid; larger bws use dashes.
    dash_map = {smallest_bw: "solid"}
    dashes = ["dash", "dot", "dashdot", "longdash", "longdashdot"]
    for i, bw in enumerate([b for b in bws if b != smallest_bw]):
        dash_map[bw] = dashes[i % len(dashes)]

    # ---- Build traces. Order: ALL raw FIRST (bottom z-order), then smoothed.
    fig = go.Figure()
    trace_meta: list[_TraceMeta] = []
    initial_visible: list[bool] = []  # default state

    # Per-trace x arrays kept side-by-side for the clock/aligned mode toggle.
    # Clock x values are ISO date strings ("YYYY-MM-DD HH:MM:SS.fff") — Plotly's
    # most reliably-parsed date input.
    x_clock_per_trace: list[list[str]] = []
    x_aligned_per_trace: list[list[float]] = []
    # Trace specs MINUS x: used by JS on mode switch to rebuild fresh trace
    # dicts free of any cached plotly internal state.
    trace_specs: list[dict] = []

    # 1) Raw pings (bottom layer, opacity 0.3, hidden by default).
    for tid in trip_ids:
        if tid not in raw_t_s:
            continue
        t_s = raw_t_s[tid]
        d_m = raw_d_m[tid]
        first_ts = pd.Timestamp(first_ping_iso[tid])
        clock_iso = (
            (first_ts + pd.to_timedelta(t_s, unit="s"))
            .strftime("%Y-%m-%d %H:%M:%S.%f")
            .str[:-3]  # truncate to milliseconds
            .tolist()
        )
        aligned_min = ((t_s - departure_s[tid]) / 60.0).tolist()
        d_mi = (d_m / _M_PER_MI).tolist()
        fig.add_trace(
            go.Scattergl(
                x=aligned_min,
                y=d_mi,
                mode="markers",
                marker={"color": trip_color[tid], "size": 7, "opacity": 0.7},
                name=f"{tid} raw",
                showlegend=False,
                hoverinfo="skip",
            )
        )
        trace_meta.append(_TraceMeta(trip=tid, bw=None, kind="raw"))
        x_aligned_per_trace.append(aligned_min)
        x_clock_per_trace.append(clock_iso)
        trace_specs.append(
            {
                "type": "scattergl",
                "mode": "markers",
                "marker": {
                    "color": trip_color[tid],
                    "size": 7,
                    "opacity": 0.7,
                },
                "name": f"{tid} raw",
                "showlegend": False,
                "hoverinfo": "skip",
                "y": d_mi,
            }
        )
        initial_visible.append(False)  # raw hidden by default

    # 2) Smoothed lines. One per (trip, bw).
    for tid in trip_ids:
        first_ts = pd.Timestamp(first_ping_iso[tid])
        for bw in bws:
            if tid not in traj_by_bw[bw]:
                continue
            f = from_pchip_record(traj_by_bw[bw][tid])
            t_grid = np.linspace(float(f.x[0]), float(f.x[-1]), n_eval)
            x_m = f(t_grid)
            v_mph = f.derivative()(t_grid) * 2.23694
            x_mi = (x_m / _M_PER_MI).tolist()
            clock_iso = (
                (first_ts + pd.to_timedelta(t_grid, unit="s"))
                .strftime("%Y-%m-%d %H:%M:%S.%f")
                .str[:-3]
                .tolist()
            )
            aligned_min = ((t_grid - departure_s[tid]) / 60.0).tolist()
            fig.add_trace(
                go.Scatter(
                    x=aligned_min,
                    y=x_mi,
                    mode="lines",
                    line={"color": trip_color[tid], "width": 1.6, "dash": dash_map[bw]},
                    name=f"{tid} bw={bw}",
                    customdata=np.stack(
                        [np.full(t_grid.size, int(tid), dtype=object), v_mph, np.full(t_grid.size, bw)],
                        axis=1,
                    ),
                    hovertemplate=(
                        "<b>trip %{customdata[0]}</b>"
                        " · bw %{customdata[2]}<br>"
                        "x: %{x:.2f}<br>"
                        "mile: %{y:.3f}<br>"
                        "speed: %{customdata[1]:.1f} mph"
                        "<extra></extra>"
                    ),
                    showlegend=True,
                    legendgroup=tid,
                    legendgrouptitle={"text": f"trip {tid} (bus {bus_id[tid]})"},
                )
            )
            trace_meta.append(_TraceMeta(trip=tid, bw=bw, kind="smooth"))
            x_aligned_per_trace.append(aligned_min)
            x_clock_per_trace.append(clock_iso)
            trace_specs.append(
                {
                    "type": "scatter",
                    "mode": "lines",
                    "line": {
                        "color": trip_color[tid],
                        "width": 1.6,
                        "dash": dash_map[bw],
                    },
                    "name": f"{tid} bw={bw}",
                    "showlegend": True,
                    "legendgroup": tid,
                    "legendgrouptitle": {
                        "text": f"trip {tid} (bus {bus_id[tid]})"
                    },
                    "customdata": [
                        [int(tid), float(v), int(bw)] for v in v_mph.tolist()
                    ],
                    "hovertemplate": (
                        "<b>trip %{customdata[0]}</b>"
                        " · bw %{customdata[2]}<br>"
                        "x: %{x}<br>"
                        "mile: %{y:.3f}<br>"
                        "speed: %{customdata[1]:.1f} mph"
                        "<extra></extra>"
                    ),
                    "y": x_mi,
                }
            )
            initial_visible.append(bw == smallest_bw)  # default check the smallest bw

    # Apply initial visibility.
    for i, vis in enumerate(initial_visible):
        fig.data[i].visible = True if vis else "legendonly"

    # ---- Bus stops (optional GTFS overlay) ------------------------------
    stops_payload: list[dict] = []
    if gtfs_zip_path is not None and pattern_id is not None:
        shape_id = shape_id_for_pattern(pattern_id)
        for s in load_route_stops(gtfs_zip_path, shape_id):
            stops_payload.append(
                {
                    "name": s["name"],
                    "stop_id": s["stop_id"],
                    "mile": s["dist_along_m"] / _M_PER_MI,
                }
            )

    fig.update_layout(
        title=title,
        xaxis_title="Minutes since departure",
        yaxis_title="Distance along route (mi)",
        hovermode="closest",
        dragmode="pan",  # pan by default; user can box-zoom from mode bar
        margin={"l": 220, "r": 30, "t": 60, "b": 50},
        template="plotly_white",
        height=chart_height_px,
    )
    fig.update_xaxes(showgrid=True, gridcolor="rgba(0,0,0,0.08)")
    fig.update_yaxes(showgrid=True, gridcolor="rgba(0,0,0,0.08)")

    # The initial render is done from the JS side via setMode() — we only need
    # plotly.js itself loaded. Use to_html on an empty figure to get the right
    # script/cdn includes, then strip the empty plot div (we provide our own).
    chart_html = pio.to_html(
        go.Figure(),
        include_plotlyjs=True if embed_js else "cdn",
        full_html=False,
        div_id="__bootstrap__",
        config={"scrollZoom": True, "displaylogo": False},
    )
    # Replace the bootstrap div with our actual chart container; the plotly.js
    # script tag (which initializes plotly globals) stays intact.
    chart_html = chart_html.replace(
        '<div id="__bootstrap__"', '<div id="__bootstrap_unused__" style="display:none"'
    )
    chart_html += '\n<div id="chart" style="height:100%;width:100%"></div>'

    meta_payload = {
        "trace_meta": [
            {"trip": m.trip, "bw": m.bw, "kind": m.kind} for m in trace_meta
        ],
        "trace_specs": trace_specs,
        "x_aligned": x_aligned_per_trace,
        "x_clock": x_clock_per_trace,
        "trips": [
            {"id": tid, "bus": bus_id[tid], "color": trip_color[tid]} for tid in trip_ids
        ],
        "bandwidths": bws,
        "stops": stops_payload,
        "raw_default": False,
        "stops_default": True,
        "x_compress": x_compress,  # visible x window = x_compress × data span
        "chart_height_px": chart_height_px,
        "title": title,
    }

    out_path = Path(out_path)
    out_path.write_text(_render_page(title, chart_html, meta_payload))
    return out_path


def _render_page(title: str, chart_html: str, meta: dict) -> str:
    """Wrap a plotly div with custom toggle controls + JS."""
    meta_json = json.dumps(meta, separators=(",", ":"))
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  body {{
    font-family: -apple-system, system-ui, sans-serif;
    margin: 0;
    display: grid;
    grid-template-columns: 280px 1fr;
    height: 100vh;
  }}
  #controls {{
    overflow-y: auto;
    padding: 16px;
    border-right: 1px solid #ddd;
    background: #fafafa;
    font-size: 13px;
  }}
  #controls h2 {{ margin: 0 0 6px; font-size: 13px; text-transform: uppercase;
                  letter-spacing: 0.05em; color: #666; }}
  #controls fieldset {{ border: none; margin: 0 0 18px; padding: 0; }}
  #controls label {{ display: block; padding: 3px 0; cursor: pointer; }}
  #controls label.dim {{ color: #aaa; }}
  #controls .swatch {{
    display: inline-block; width: 10px; height: 10px;
    margin-right: 6px; border-radius: 2px; vertical-align: middle;
  }}
  #controls .row {{ display: flex; gap: 8px; }}
  #controls button {{
    font-size: 11px; padding: 4px 8px; border: 1px solid #ccc;
    background: white; cursor: pointer; border-radius: 3px;
  }}
  #controls button:hover {{ background: #eef; }}
  #chart-container {{ position: relative; }}
  #chart {{ height: 100%; width: 100%; }}
</style>
</head>
<body>
<div id="controls">
  <h2>Trips</h2>
  <fieldset id="trip-controls"></fieldset>
  <div class="row">
    <button id="trips-all">All</button>
    <button id="trips-none">None</button>
  </div>
  <h2 style="margin-top:18px">Bandwidth</h2>
  <fieldset id="bw-controls"></fieldset>
  <div class="row">
    <button id="bws-all">All</button>
    <button id="bws-none">None</button>
  </div>
  <h2 style="margin-top:18px">Display</h2>
  <fieldset>
    <label><input type="checkbox" id="raw-toggle"> Raw GPS pings (70% opacity)</label>
    <label><input type="checkbox" id="stops-toggle" checked> Bus stops (with labels)</label>
  </fieldset>
  <h2 style="margin-top:18px">X-axis</h2>
  <fieldset>
    <label><input type="radio" name="xmode" value="aligned" checked> Aligned by departure (min)</label>
    <label><input type="radio" name="xmode" value="clock"> Clock time</label>
  </fieldset>
  <h2 style="margin-top:18px">Tip</h2>
  <div style="font-size:11px;color:#666;line-height:1.4">
    Drag chart to pan. Scroll to zoom. Mode bar (top right) has box-zoom and
    reset. Stop labels stay anchored at the left edge as you pan/zoom.
  </div>
</div>
<div id="chart-container">
  {chart_html}
</div>
<script>
const META = {meta_json};
function el(tag, attrs, ...kids) {{
  const e = document.createElement(tag);
  for (const k in attrs) {{
    if (k === "html") e.innerHTML = attrs[k];
    else e[k] = attrs[k];
  }}
  for (const k of kids) {{
    if (typeof k === "string") e.appendChild(document.createTextNode(k));
    else if (k) e.appendChild(k);
  }}
  return e;
}}

// --- build trip checkboxes ---
const tripFs = document.getElementById("trip-controls");
META.trips.forEach(t => {{
  const cb = el("input", {{type: "checkbox", checked: true, dataset_trip: t.id}});
  cb.dataset.trip = t.id;
  const swatch = el("span"); swatch.className = "swatch";
  swatch.style.background = t.color;
  const lab = el("label", {{}}, cb, swatch, ` ${{t.id}} (bus ${{t.bus}})`);
  tripFs.appendChild(lab);
}});

// --- build bandwidth checkboxes ---
const bwFs = document.getElementById("bw-controls");
META.bandwidths.forEach((bw, i) => {{
  const cb = el("input", {{type: "checkbox", checked: i === 0, dataset_bw: bw}});
  cb.dataset.bw = String(bw);
  const lab = el("label", {{}}, cb, ` bw=${{bw}}`);
  bwFs.appendChild(lab);
}});

const chart = document.getElementById("chart");

function visibilityForTrace(m) {{
  const tripsOn = new Set([...document.querySelectorAll("[data-trip]:checked")].map(c => c.dataset.trip));
  const bwsOn = new Set([...document.querySelectorAll("[data-bw]:checked")].map(c => c.dataset.bw));
  const rawOn = document.getElementById("raw-toggle").checked;
  if (!tripsOn.has(m.trip)) return false;
  if (m.kind === "raw") return rawOn ? true : false;
  return bwsOn.has(String(m.bw)) ? true : false;
}}

function updateVisibility() {{
  const vis = META.trace_meta.map(visibilityForTrace);
  Plotly.restyle(chart, {{visible: vis}});
}}

// ---- X-axis mode (clock / aligned) ----
// Strategy: rebuild the chart from scratch on every mode change. We never
// reuse plotly's internal trace state (which caches axis-type per trace and
// has bitten earlier attempts to swap data + axis-type together). Instead we
// keep clean trace templates in META.trace_specs and rebuild fresh dicts.
function buildBaseLayout(mode) {{
  return {{
    title: META.title,
    xaxis: {{
      title: {{text: mode === "clock" ? "Time of day" : "Minutes since departure"}},
      type: mode === "clock" ? "date" : "linear",
      showgrid: true,
      gridcolor: "rgba(0,0,0,0.08)",
      autorange: true,
    }},
    yaxis: {{
      title: {{text: "Distance along route (mi)"}},
      showgrid: true,
      gridcolor: "rgba(0,0,0,0.08)",
    }},
    hovermode: "closest",
    dragmode: "pan",
    margin: {{l: 220, r: 30, t: 60, b: 50}},
    template: "plotly_white",
    height: META.chart_height_px,
  }};
}}

function buildData(mode) {{
  const xs = mode === "clock" ? META.x_clock : META.x_aligned;
  // Hover format for the x readout: time-of-day for clock, numeric for aligned.
  const xFmt = mode === "clock" ? "%{{x|%H:%M:%S}}" : "%{{x:.2f}} min";
  return META.trace_specs.map((spec, i) => {{
    const t = JSON.parse(JSON.stringify(spec));
    t.x = xs[i];
    if (t.hovertemplate) {{
      t.hovertemplate = t.hovertemplate.replace("%{{x}}", xFmt);
    }}
    return t;
  }});
}}

function setMode(mode) {{
  const data = buildData(mode);
  const layout = buildBaseLayout(mode);
  // purge wipes ALL plotly state on the div; newPlot rebuilds cleanly.
  Plotly.purge(chart);
  Plotly.newPlot(chart, data, layout, {{scrollZoom: true, displaylogo: false}});
  applyXCompression();
  updateStops();
  updateVisibility();
}}

// ---- Stop overlay ----
function buildStopLayout(showStops) {{
  if (!showStops || !META.stops || META.stops.length === 0) {{
    return {{shapes: [], annotations: []}};
  }}
  const shapes = META.stops.map(s => ({{
    type: "line",
    xref: "paper", x0: 0, x1: 1,
    yref: "y", y0: s.mile, y1: s.mile,
    line: {{color: "rgba(80,80,80,0.45)", width: 0.6, dash: "dot"}},
    layer: "below",
  }}));
  const annotations = META.stops.map(s => ({{
    xref: "paper", x: 0, xanchor: "right",
    yref: "y", y: s.mile, yanchor: "middle",
    text: s.name,
    showarrow: false,
    font: {{size: 9, color: "#444"}},
    align: "right",
    bgcolor: "rgba(255,255,255,0.85)",
    borderpad: 1,
    xshift: -6,
  }}));
  return {{shapes, annotations}};
}}

function updateStops() {{
  const show = document.getElementById("stops-toggle").checked;
  const {{shapes, annotations}} = buildStopLayout(show);
  Plotly.relayout(chart, {{shapes, annotations}});
}}

// ---- X-axis compression ----
// Plotly's autorange sets the visible x to roughly [data_min, data_max].
// We "compress" by extending the range to ~2x the data span; this halves the
// effective px-per-unit. The chart also has dragmode='pan' so the user can
// move freely.
function applyXCompression() {{
  if (!META.x_compress || META.x_compress === 1) return;
  const factor = META.x_compress;
  const ax = chart.layout.xaxis;
  if (!ax || !ax.range) return;
  // For date axes, range entries are strings ("YYYY-MM-DD HH:MM:SS"); coerce
  // to epoch ms for arithmetic. Plotly accepts ms numbers as range values too,
  // so we pass numbers back regardless of axis type.
  const toNum = v => (typeof v === "string") ? new Date(v).getTime() : Number(v);
  const lo = toNum(ax.range[0]);
  const hi = toNum(ax.range[1]);
  if (!isFinite(lo) || !isFinite(hi)) return;
  const span = hi - lo;
  const cx = (lo + hi) / 2;
  const newSpan = span * factor;
  Plotly.relayout(chart, {{"xaxis.range": [cx - newSpan/2, cx + newSpan/2]}});
}}

document.querySelectorAll("[data-trip], [data-bw], #raw-toggle").forEach(c => {{
  c.addEventListener("change", updateVisibility);
}});
document.getElementById("stops-toggle").addEventListener("change", updateStops);
document.querySelectorAll("[name=xmode]").forEach(r => {{
  r.addEventListener("change", () => setMode(r.value));
}});

document.getElementById("trips-all").onclick = () => {{
  document.querySelectorAll("[data-trip]").forEach(c => c.checked = true);
  updateVisibility();
}};
document.getElementById("trips-none").onclick = () => {{
  document.querySelectorAll("[data-trip]").forEach(c => c.checked = false);
  updateVisibility();
}};
document.getElementById("bws-all").onclick = () => {{
  document.querySelectorAll("[data-bw]").forEach(c => c.checked = true);
  updateVisibility();
}};
document.getElementById("bws-none").onclick = () => {{
  document.querySelectorAll("[data-bw]").forEach(c => c.checked = false);
  updateVisibility();
}};

// Initial render: build the chart via the same code path used for mode switches.
const initialMode = document.querySelector("[name=xmode]:checked").value;
setMode(initialMode);
</script>
</body>
</html>
"""
