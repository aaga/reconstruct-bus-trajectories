"""Leaflet HTML map of one shape's controlled features.

Three layers:
  - Gray polyline: the GTFS shape.
  - Color-coded markers at each ControlPoint:
        amber circle   = traffic_signals
        red octagon    = stop sign
        purple circle  = ped_crossing_signal (signalised pedestrian crossing)
        teal circle    = ped_crossing_marked (marked but unsignalised)
  - Tooltips (on hover) show cross-street name(s) or mile location,
    dist_along_route_m, and the bus's way_id. Clicking a marker opens
    Google Street View in a new tab, with the camera pointed in the
    configured heading (default 180° = south, matching SB Route 22).

Run:
    PYTHONPATH=src .venv/bin/python scripts/plot_intersections.py \\
        --intersections intersections_route22.json \\
        --gtfs cta_gtfs.zip \\
        --shape-id 67803936 \\
        --heading 180 \\
        --out intersections_22sb.html
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from bus_trajectories.intersections import ControlPoint, load_intersections
from bus_trajectories.io import load_gtfs_shape_with_dist


_COLORS = {
    "traffic_signals": "#dc8c32",       # amber circle
    "stop": "#cc0000",                  # red octagon (SVG below)
    "ped_crossing_signal": "#7b3fa0",   # purple circle — signalised crosswalk
    "ped_crossing_marked": "#00897b",   # teal circle — marked crosswalk
}


_LATMETER = 111320.0  # meters per degree of latitude (and approximate per
                       # degree of longitude at the equator); for Chicago lats
                       # the lon-component is rescaled by cos(lat).


def _dist_m(a, b) -> float:
    """Equirectangular metres between two ControlPoint-shaped objects."""
    import math
    dy = (a.lat - b.lat) * _LATMETER
    dx = (a.lon - b.lon) * _LATMETER * math.cos(math.radians((a.lat + b.lat) / 2))
    return math.hypot(dx, dy)


def _compute_diff(primary: list, compare: list) -> tuple[list, list]:
    """Return (primary_only, compare_only) — the features that appear in one
    shape's enrichment but not the other. A ControlPoint matches a partner if
    EITHER (a) within 30 m of one of the same control_type, OR
    (b) shares the same (control_type, frozenset(cross_street_names))
    when both have a non-empty cross-street set."""
    # Geographic match (greedy, ≤30 m, same control_type)
    cmp_used: set[int] = set()
    matched_p: set[int] = set()
    for i, p in enumerate(primary):
        cands = sorted(
            (_dist_m(p, c), j)
            for j, c in enumerate(compare)
            if c.control_type == p.control_type and j not in cmp_used
        )
        if cands and cands[0][0] <= 30.0:
            matched_p.add(i)
            cmp_used.add(cands[0][1])
    # Name match (additive — catches divided-arterial pairs where the same
    # named intersection sits ~250 m apart geographically)
    name_p: dict[tuple, list[int]] = {}
    name_c: dict[tuple, list[int]] = {}
    for i, p in enumerate(primary):
        if not p.cross_street_names:
            continue
        name_p.setdefault(
            (p.control_type, frozenset(p.cross_street_names)), []
        ).append(i)
    for j, c in enumerate(compare):
        if not c.cross_street_names:
            continue
        name_c.setdefault(
            (c.control_type, frozenset(c.cross_street_names)), []
        ).append(j)
    for k in set(name_p) | set(name_c):
        ps, cs = name_p.get(k, []), name_c.get(k, [])
        n_pair = min(len(ps), len(cs))
        for i in ps[:n_pair]:
            matched_p.add(i)
        for j in cs[:n_pair]:
            cmp_used.add(j)
    primary_only = [p for i, p in enumerate(primary) if i not in matched_p]
    compare_only = [c for j, c in enumerate(compare) if j not in cmp_used]
    return primary_only, compare_only


def _street_view_url(lat: float, lon: float, heading: float) -> str:
    """Google Maps Street View URL pointing at heading (deg, 0=N, 90=E, 180=S, 270=W)."""
    # The /data=!3m6!1e1!3m4!1s...!2e0 suffix tells Google Maps "open in
    # Street View immediately" rather than the usual map view. Using just
    # the @lat,lon,3a notation alone often falls back to the map.
    return (
        f"https://www.google.com/maps/@{lat:.6f},{lon:.6f},3a,75y,"
        f"{heading:.1f}h,90t/data=!3m1!1e1"
    )


def build(
    intersections_path: Path,
    gtfs_zip: Path,
    shape_id: str,
    out_path: Path,
    heading: float = 180.0,
    compare_shape_id: str | None = None,
    compare_heading: float | None = None,
    primary_label: str = "primary",
    compare_label: str = "compare",
) -> None:
    data = load_intersections(intersections_path)
    if shape_id not in data:
        raise SystemExit(f"shape_id {shape_id} not in intersections file")

    poly, _ = load_gtfs_shape_with_dist(gtfs_zip, shape_id)
    polyline = [[float(lat), float(lon)] for lat, lon in poly.tolist()]

    compare_polyline: list[list[float]] | None = None
    if compare_shape_id is not None:
        if compare_shape_id not in data:
            raise SystemExit(
                f"compare shape_id {compare_shape_id} not in intersections file"
            )
        cmp_poly, _ = load_gtfs_shape_with_dist(gtfs_zip, compare_shape_id)
        compare_polyline = [[float(lat), float(lon)] for lat, lon in cmp_poly.tolist()]
        # Default compare heading is the inverse of primary (so a NB direction
        # of 0 mirrors a SB heading of 180).
        if compare_heading is None:
            compare_heading = (heading + 180.0) % 360.0
        primary_diff, compare_diff = _compute_diff(data[shape_id], data[compare_shape_id])
    else:
        primary_diff = data[shape_id]
        compare_diff = []

    # Map centre = midpoint of all polyline vertices in both shapes.
    all_pts = polyline + (compare_polyline or [])
    centre = [
        sum(p[0] for p in all_pts) / len(all_pts),
        sum(p[1] for p in all_pts) / len(all_pts),
    ]

    counts: dict[str, int] = {}
    points: list[dict] = []

    def _add(cps: list, which: str, label: str, head: float) -> None:
        for cp in cps:
            counts[cp.control_type] = counts.get(cp.control_type, 0) + 1
            points.append({
                "node_id": cp.intersection_node_id,
                "lat": cp.lat,
                "lon": cp.lon,
                "type": cp.control_type,
                "color": _COLORS.get(cp.control_type, "#888"),
                "dist_mi": cp.dist_along_route_m / 1609.344,
                "on_way_id": cp.on_way_id,
                "cross_streets": list(cp.cross_street_names),
                "street_view_url": _street_view_url(cp.lat, cp.lon, head),
                "anchor_id": cp.anchor_intersection_node_id,
                "signalized": cp.signalized,
                "markings": cp.markings,
                "has_island": cp.has_island,
                "merged_ids": list(cp.merged_node_ids),
                "which": which,         # "primary" | "compare"
                "dir_label": label,     # human label (e.g. "SB", "NB")
            })

    _add(primary_diff, "primary", primary_label, heading)
    _add(compare_diff, "compare", compare_label, compare_heading or 0.0)

    payload = {
        "shape_id": shape_id,
        "compare_shape_id": compare_shape_id,
        "primary_label": primary_label,
        "compare_label": compare_label,
        "diff_mode": compare_shape_id is not None,
        "centre": centre,
        "polyline": polyline,
        "compare_polyline": compare_polyline,
        "points": points,
        "counts": counts,
        "n_total": len(points),
        "heading": heading,
        "compare_heading": compare_heading,
        "n_primary_diff": len(primary_diff),
        "n_compare_diff": len(compare_diff),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(_render_html(payload))
    summary = ", ".join(f"{n} {t}" for t, n in sorted(counts.items())) or "none"
    print(f"[plot] saved: {out_path}")
    if compare_shape_id is not None:
        print(f"[plot]   diff: {primary_label}({shape_id}) vs "
              f"{compare_label}({compare_shape_id})")
        print(f"[plot]   {primary_label}-only: {len(primary_diff)}  "
              f"{compare_label}-only: {len(compare_diff)}  ({summary})")
    else:
        print(f"[plot]   shape {shape_id}: {len(points)} intersections ({summary})")


def _render_html(p: dict) -> str:
    js = json.dumps(p, separators=(",", ":"))
    return f"""<!DOCTYPE html>
<html><head>
<title>Intersections: shape {p['shape_id']}</title>
<meta charset="utf-8">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
<style>
  body {{ margin: 0; font-family: -apple-system, system-ui, sans-serif; }}
  #header {{ position: absolute; top: 0; left: 0; right: 0; padding: 8px 16px;
              background: #fff; border-bottom: 1px solid #ccc; z-index: 1000;
              font-size: 12px; }}
  #map {{ position: absolute; top: 50px; left: 0; right: 0; bottom: 0; }}
  .swatch {{ display: inline-block; width: 11px; height: 11px;
              vertical-align: middle; margin-right: 4px;
              border: 1px solid #555; }}
  .swatch-hex {{ width: 12px; height: 12px; background: #cc0000;
                  border: 1px solid #000;
                  /* Regular octagon: corner cut at c/S = 1 - 1/(1+sqrt(2)) ≈ 29.29%. */
                  clip-path: polygon(29.29% 0%, 70.71% 0%, 100% 29.29%, 100% 70.71%,
                                     70.71% 100%, 29.29% 100%, 0% 70.71%, 0% 29.29%); }}
  .leaflet-marker-icon.stop-hex {{ background: transparent !important;
                                    border: none !important; }}
</style>
</head><body>
<div id="header">
  <strong><span id="title"></span></strong>
  &middot; <span id="n"></span> features
  &middot; <span class="swatch" style="background:#dc8c32;border-radius:50%"></span> signal: <span id="c-signals"></span>
  &middot; <span class="swatch swatch-hex"></span> stop: <span id="c-stop"></span>
  &middot; <span class="swatch" style="background:#7b3fa0;border-radius:50%"></span> ped signal: <span id="c-pedsig"></span>
  &middot; <span class="swatch" style="background:#00897b;border-radius:50%"></span> crosswalk: <span id="c-pedmark"></span>
  <span id="line-legend"></span>
  &middot; <span style="color:#666">hover for details · click → Street View · hold → copy node/way id</span>
</div>
<div id="toast" style="position:fixed;top:60px;left:50%;transform:translateX(-50%);
     background:#222;color:#fff;padding:8px 14px;border-radius:4px;font-size:13px;
     z-index:10000;box-shadow:0 2px 8px rgba(0,0,0,0.3);
     opacity:0;pointer-events:none;transition:opacity 0.15s ease-out;"></div>
<div id="map"></div>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const D = {js};
const PRIMARY_COLOR = "#1f4e79";   // SB-ish — dark blue
const COMPARE_COLOR = "#c0392b";   // NB-ish — red/orange
document.getElementById("title").textContent = D.diff_mode
  ? `${{D.primary_label}}(${{D.shape_id}}) vs ${{D.compare_label}}(${{D.compare_shape_id}}) — only differing features`
  : `Shape ${{D.shape_id}}`;
document.getElementById("n").textContent = D.diff_mode
  ? `${{D.n_primary_diff}} ${{D.primary_label}}-only + ${{D.n_compare_diff}} ${{D.compare_label}}-only`
  : D.n_total;
document.getElementById("c-signals").textContent = D.counts.traffic_signals || 0;
document.getElementById("c-stop").textContent = D.counts.stop || 0;
document.getElementById("c-pedsig").textContent = D.counts.ped_crossing_signal || 0;
document.getElementById("c-pedmark").textContent = D.counts.ped_crossing_marked || 0;
if (D.diff_mode) {{
  const ll = document.getElementById("line-legend");
  ll.innerHTML =
    ' &middot; <span style="display:inline-block;width:16px;height:3px;background:' +
    PRIMARY_COLOR + ';vertical-align:middle;margin-right:4px"></span>' + D.primary_label +
    ' route &middot; <span style="display:inline-block;width:16px;height:3px;background:' +
    COMPARE_COLOR + ';vertical-align:middle;margin-right:4px"></span>' + D.compare_label + ' route';
}}

const map = L.map("map").setView(D.centre, 12);
// CARTO Positron tiles — permissive license, no Referer header required
// (the standard tile.openstreetmap.org server blocks file:// because it
// can't validate a Referer).
L.tileLayer("https://{{s}}.basemaps.cartocdn.com/light_all/{{z}}/{{x}}/{{y}}{{r}}.png", {{
  maxZoom: 19,
  subdomains: "abcd",
  attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
}}).addTo(map);

// Primary polyline (and compare polyline if in diff mode). Distinct colours
// so the two routes are easy to tell apart in the Loop where they diverge.
L.polyline(D.polyline, {{
  color: D.diff_mode ? PRIMARY_COLOR : "#666", weight: 4, opacity: 0.75
}}).addTo(map);
if (D.compare_polyline) {{
  L.polyline(D.compare_polyline, {{
    color: COMPARE_COLOR, weight: 4, opacity: 0.75
  }}).addTo(map);
}}

// Stop-sign SVG: regular octagon (all 8 sides equal length). For a 22x22
// box, the exact corner cut is c = 22 / (2 + √2) ≈ 6.4434, giving every
// side length ≈ 9.113 px.
function stopIcon(fill) {{
  const svg =
    '<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 22 22">' +
    '<polygon points="6.4434,0 15.5566,0 22,6.4434 22,15.5566 15.5566,22 6.4434,22 0,15.5566 0,6.4434" ' +
    'fill="' + fill + '" stroke="#000" stroke-width="1"/></svg>';
  return L.divIcon({{
    html: svg,
    className: "stop-hex",
    iconSize: [22, 22],
    iconAnchor: [11, 11],
  }});
}}

const stopOcto = stopIcon("#cc0000");

const TYPE_LABEL = {{
  traffic_signals: "Traffic Signal",
  stop: "Stop Sign",
  ped_crossing_signal: "Pedestrian Signal",
  ped_crossing_marked: "Marked Crosswalk",
}};

const toastEl = document.getElementById("toast");
let toastTimer = null;
function showToast(msg, isError) {{
  toastEl.textContent = msg;
  toastEl.style.background = isError ? "#cc3333" : "#222";
  toastEl.style.opacity = "1";
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {{ toastEl.style.opacity = "0"; }}, 1800);
}}

function copyIds(p) {{
  const text = `node_id: ${{p.node_id}}\\nway_id: ${{p.on_way_id}}`;
  const ok = () => showToast(`Copied  node_id=${{p.node_id}}  way_id=${{p.on_way_id}}`);
  const fail = (e) => showToast(`Copy failed: ${{e}}`, true);
  if (navigator.clipboard && navigator.clipboard.writeText) {{
    navigator.clipboard.writeText(text).then(ok, fail);
  }} else {{
    // Fallback: hidden textarea + execCommand (older browsers)
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    try {{ document.execCommand("copy"); ok(); }}
    catch (e) {{ fail(e); }}
    finally {{ ta.remove(); }}
  }}
}}

const LONG_PRESS_MS = 450;

D.points.forEach(p => {{
  const cross = p.cross_streets.length ? p.cross_streets.join(" / ") : "(mid-block)";
  const label = TYPE_LABEL[p.type] || p.type;
  // Direction badge: only shown in diff mode (when each marker carries a
  // dir_label and which-side flag). Coloured to match the route polyline.
  const dirColor = D.diff_mode
    ? (p.which === "primary" ? PRIMARY_COLOR : COMPARE_COLOR)
    : null;
  const dirBadge = D.diff_mode
    ? `<div style="display:inline-block;padding:2px 6px;border-radius:3px;` +
      `background:${{dirColor}};color:#fff;font-size:11px;` +
      `font-weight:bold;margin-bottom:2px">${{p.dir_label}}-only</div><br>`
    : "";
  const lines = [
    `${{dirBadge}}<b>${{label}}</b>`,
    `mile: ${{p.dist_mi.toFixed(2)}}`,
    `node_id: ${{p.node_id}}`,
    `bus way_id: ${{p.on_way_id}}`,
  ];
  if (p.type === "traffic_signals" || p.type === "stop") {{
    lines.splice(2, 0, `cross: ${{cross}}`);
    if (p.merged_ids && p.merged_ids.length) {{
      lines.push(`merged nodes: ${{p.merged_ids.join(", ")}}`);
    }}
  }} else {{
    // pedestrian crossings
    lines.push(`anchor vertex: ${{p.anchor_id !== null ? p.anchor_id : "(mid-block)"}}`);
    lines.push(`signalised: ${{p.signalized ? "yes" : "no"}}`);
    lines.push(`markings: ${{p.markings || "(none)"}}`);
    lines.push(`island: ${{p.has_island ? "yes" : "no"}}`);
  }}
  const tooltipHtml = lines.join("<br>");

  // In diff mode, give each marker a coloured halo matching its route
  // direction so it's visually identifiable even without the tooltip.
  const stroke = D.diff_mode ? dirColor : "#000";
  const strokeWeight = D.diff_mode ? 3 : 1;

  let m;
  if (p.type === "stop") {{
    m = L.marker([p.lat, p.lon], {{ icon: stopOcto }});
  }} else {{
    m = L.circleMarker([p.lat, p.lon], {{
      radius: 7, color: stroke, weight: strokeWeight,
      fillColor: p.color, fillOpacity: 0.9
    }});
  }}
  m.bindTooltip(tooltipHtml, {{ sticky: true }});

  // Hold-to-copy vs. click-to-streetview.
  // mousedown starts a 450 ms timer that, if it fires before mouseup,
  // copies the node/way ids to the clipboard and marks the gesture so
  // the trailing click event is suppressed. Otherwise a normal click
  // launches Street View.
  let pressTimer = null;
  let longPressed = false;
  m.on("mousedown", () => {{
    longPressed = false;
    pressTimer = setTimeout(() => {{
      longPressed = true;
      copyIds(p);
    }}, LONG_PRESS_MS);
  }});
  const cancelPress = () => {{
    if (pressTimer) {{ clearTimeout(pressTimer); pressTimer = null; }}
  }};
  m.on("mouseup", cancelPress);
  m.on("mouseout", cancelPress);
  m.on("click", () => {{
    if (longPressed) {{ longPressed = false; return; }}
    window.open(p.street_view_url, "_blank", "noopener");
  }});
  // Right-click is a natural "advanced" gesture — make it also copy,
  // and stop the browser's context menu.
  m.on("contextmenu", (e) => {{
    if (e.originalEvent) e.originalEvent.preventDefault();
    copyIds(p);
  }});
  m.addTo(map);
}});

map.fitBounds(L.polyline(D.polyline).getBounds(), {{padding: [20, 20]}});
</script>
</body></html>"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--intersections", required=True)
    ap.add_argument("--gtfs", required=True)
    ap.add_argument("--shape-id", required=True, dest="shape_id")
    ap.add_argument("--out", default="intersections_map.html")
    ap.add_argument(
        "--heading",
        type=float,
        default=180.0,
        help="Street View camera heading for primary shape markers in "
             "degrees (0=N, 90=E, 180=S, 270=W). Default 180 (south).",
    )
    ap.add_argument(
        "--compare-shape-id",
        default=None,
        dest="compare_shape_id",
        help="If set, renders BOTH shapes' polylines and shows ONLY the "
             "features that differ between them (the primary-only and "
             "compare-only sets, using a geographic-OR-name matcher).",
    )
    ap.add_argument(
        "--compare-heading",
        type=float,
        default=None,
        dest="compare_heading",
        help="Street View heading for the compare shape's markers. "
             "Defaults to the inverse of --heading.",
    )
    ap.add_argument(
        "--primary-label", default="primary", dest="primary_label",
        help="Short label for the primary shape in tooltips/legend (e.g. SB).",
    )
    ap.add_argument(
        "--compare-label", default="compare", dest="compare_label",
        help="Short label for the compare shape in tooltips/legend (e.g. NB).",
    )
    args = ap.parse_args()
    build(
        Path(args.intersections), Path(args.gtfs), args.shape_id,
        Path(args.out),
        heading=args.heading,
        compare_shape_id=args.compare_shape_id,
        compare_heading=args.compare_heading,
        primary_label=args.primary_label,
        compare_label=args.compare_label,
    )


if __name__ == "__main__":
    main()
