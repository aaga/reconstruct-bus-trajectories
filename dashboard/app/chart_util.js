// Stateless chart plumbing + helpers shared by the trajectory and speed views.
// Extracted verbatim from the original monolithic main.js; the only change is
// that the S-dependent extent helpers now take the shared state explicitly.

import * as d3 from "https://cdn.jsdelivr.net/npm/d3@7.9.0/+esm";

export const $ = (id) => document.getElementById(id);

// Bus icon, source-coloured: High-Freq green, Low-Freq yellow.
export function busElement(fill) {
  const el = document.createElement("div");
  el.className = "cursor-bus";
  el.style.display = "none";
  el.innerHTML = `
    <svg viewBox="0 0 36 24" width="32" height="22" aria-hidden="true">
      <rect x="1" y="2" width="32" height="16" rx="3" fill="${fill}" stroke="#222" stroke-width="1.1"/>
      <rect x="3" y="5" width="4" height="5" fill="#bce3ff" stroke="#222" stroke-width="0.7"/>
      <rect x="8.5" y="5" width="4" height="5" fill="#bce3ff" stroke="#222" stroke-width="0.7"/>
      <rect x="14" y="5" width="4" height="5" fill="#bce3ff" stroke="#222" stroke-width="0.7"/>
      <rect x="19.5" y="5" width="4" height="5" fill="#bce3ff" stroke="#222" stroke-width="0.7"/>
      <path d="M 25 4 L 32 5 L 32 11 L 25 11 Z" fill="#bce3ff" stroke="#222" stroke-width="0.7"/>
      <circle cx="7" cy="20" r="3" fill="#222"/><circle cx="7" cy="20" r="1.3" fill="#888"/>
      <circle cx="27" cy="20" r="3" fill="#222"/><circle cx="27" cy="20" r="1.3" fill="#888"/>
    </svg>`;
  return el;
}

// linear interp on ascending xs
export function interp(xs, ys, xq) {
  if (xq <= xs[0]) return ys[0];
  if (xq >= xs[xs.length - 1]) return ys[ys.length - 1];
  let lo = 0, hi = xs.length - 1;
  while (hi - lo > 1) { const m = (lo + hi) >> 1; if (xs[m] <= xq) lo = m; else hi = m; }
  const f = (xq - xs[lo]) / (xs[hi] - xs[lo] || 1);
  return ys[lo] + f * (ys[hi] - ys[lo]);
}

export const CAT_COLOR = {
  // inferred (delay_decomposition) categories
  dwell: "#3a85d6", dwell_near_signal: "#2b6cb0",
  signal_uniform: "#cc0000", signal_overflow: "#7d1010",
  crossing: "#00897b", slowdown: "#b27ab2",
  // web-app event types
  bus_stop: "#3a85d6", red_light: "#cc0000", congestion: "#e0a800",
  turn_delay: "#7b3fa0", driver_hold: "#5c6bc0", passenger: "#d81b60",
  exit_wait: "#5c8ab8", other: "#757575", signal: "#cc0000",
  // AVL stop layer: serviced stops vs other rows with dwell
  avl_stop: "#00897b", avl_other: "#e8710a",
};
export const color = (c) => CAT_COLOR[c] || "#999";

export function fmtClock(trip, tSec) {
  const d = new Date(trip.t0_epoch_ms + tSec * 1000);
  return d.toLocaleTimeString("en-US", { hour12: false, timeZone: "America/Chicago" });
}

// Unified-schema source access. The "primary" source (phone if present, else
// the first) sets the cropped default extents, as the phone GPS window did.
export const getSource = (trip, key) => trip.sources.find((s) => s.key === key);
export const primaryCurve = (trip) => (getSource(trip, "phone") || trip.sources[0]).curve;

export function timeExtent(trip) {
  let lo = Infinity, hi = -Infinity;
  const span = (arr) => arr.forEach((v) => { if (v != null) { lo = Math.min(lo, v); hi = Math.max(hi, v); } });
  for (const s of trip.sources) span([s.curve.t[0], s.curve.t.at(-1)]);
  // Match the original: inferred delays contribute [t_start,t_end]; observed
  // rows [t_start, t_end ?? t_start+5]; AVL rows don't extend the full window.
  for (const row of trip.delay_rows) {
    if (row.role === "avl") continue;
    const open = row.role === "observed";
    row.items.forEach((d) => span([d.t_start, open ? (d.t_end ?? d.t_start + 5) : d.t_end]));
  }
  if (!isFinite(lo)) { lo = 0; hi = 1; }
  return [lo, hi];
}

export function pad([lo, hi], frac = 0.03) {
  const d = (hi - lo) * frac || 1;
  return [lo - d, hi + d];
}

// Default x-extent: the primary-source window (cropped) unless "show full trip".
export function defaultXExtent(S) {
  if (S.showFull) return timeExtent(S.trip);
  const c = primaryCurve(S.trip);
  return pad([c.t[0], c.t.at(-1)], 0.02);
}

// Default x-extent (meters) for the speed tab in distance mode.
export function defaultDistExtentM(S) {
  const t = S.trip;
  if (S.showFull) {
    let m = 0;
    for (const s of t.sources) m = Math.max(m, d3.max(s.curve.dist_m));
    return [0, m];
  }
  const dm = primaryCurve(t).dist_m;
  return pad([d3.min(dm), d3.max(dm)], 0.02);
}

// Default y-extent (km) for the trajectory tab: primary-source distance span
// when cropped, the whole route when showing the full trip.
export function defaultYExtentKm(S) {
  const t = S.trip;
  if (S.showFull) {
    let maxD = 0;
    for (const s of t.sources) maxD = Math.max(maxD, d3.max(s.curve.dist_m));
    return [0, maxD / 1000];
  }
  const dm = primaryCurve(t).dist_m;
  return pad([d3.min(dm) / 1000, d3.max(dm) / 1000], 0.04);
}

// ------------------------------------------------------- pan/zoom plumbing

export function clampDom([lo, hi], full) {
  const fspan = full[1] - full[0];
  if (hi - lo >= fspan) return full.slice();
  if (lo < full[0]) { hi += full[0] - lo; lo = full[0]; }
  if (hi > full[1]) { lo -= hi - full[1]; hi = full[1]; }
  return [lo, hi];
}

export function zoomAxis(scale, dom, full, pixel, factor) {
  const v = scale.invert(pixel);
  const minSpan = (full[1] - full[0]) / 600;
  let lo = v - (v - dom[0]) * factor;
  let hi = v + (dom[1] - v) * factor;
  if (hi - lo < minSpan) { const m = (lo + hi) / 2; lo = m - minSpan / 2; hi = m + minSpan / 2; }
  return clampDom([lo, hi], full);
}

// state = { x:{scale,full}, y:{scale,full}|null }, store = the per-tab view obj
// to mutate, axisMode(e)->{x,y}, panY bool, redraw()
export function installInteraction(svg, state, store, axisMode, panY, redraw) {
  const node = svg.node();

  node.addEventListener("wheel", (e) => {
    e.preventDefault();
    const mode = axisMode(e);
    const factor = Math.pow(1.0015, e.deltaY); // up=zoom-in (<1), down=zoom-out
    const [mx, my] = d3.pointer(e, node);
    if (mode.x) store.x = zoomAxis(state.x.scale, store.x, state.x.full, mx, factor);
    if (mode.y && state.y) store.y = zoomAxis(state.y.scale, store.y, state.y.full, my, factor);
    redraw();
  }, { passive: false });

  const drag = d3.drag()
    .on("start", () => { node._dragMoved = false; })
    .on("drag", (e) => {
      if (Math.abs(e.dx) + Math.abs(e.dy) > 2) node._dragMoved = true;
      const [xlo, xhi] = store.x;
      const W = state.x.scale.range()[1] - state.x.scale.range()[0];
      store.x = clampDom([xlo - e.dx * (xhi - xlo) / W, xhi - e.dx * (xhi - xlo) / W], state.x.full);
      if (panY && state.y) {
        const [ylo, yhi] = store.y;
        const r = state.y.scale.range();
        const H = Math.abs(r[0] - r[1]);
        store.y = clampDom([ylo + e.dy * (yhi - ylo) / H, yhi + e.dy * (yhi - ylo) / H], state.y.full);
      }
      redraw();
    });
  svg.style("cursor", "grab").call(drag);
}

export function makeSvg() {
  $("chart").innerHTML = "";
  const width = $("chart").clientWidth || 900;
  const height = Math.max(260, $("chart").clientHeight || 460);
  const svg = d3.select("#chart").append("svg")
    .attr("width", width).attr("height", height)
    .attr("viewBox", `0 0 ${width} ${height}`)
    .style("user-select", "none").style("display", "block");
  return { svg, width, height };
}
