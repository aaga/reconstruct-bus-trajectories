// Phone-vs-R2 trip comparison dashboard. Two tabs over a shared UTC wall-clock
// x-axis (seconds since the trip's t0, labelled in Chicago local time):
//   trajectory — distance-along-route vs time: phone(bw20) + R2(bw5) smoothed
//     curves, optional raw map-matched pings, optional stop lines.
//   speed      — speed vs time, with three stacked delay-bar rows
//     (web app / phone-inferred / R2-inferred), each toggleable.
//
// Interaction:
//   trajectory — wheel zooms both axes; SHIFT = vertical only,
//                CMD/CTRL = horizontal only; drag pans.
//   speed      — wheel/drag are horizontal only (time).
// Both charts fill the available window height.

import * as d3 from "https://cdn.jsdelivr.net/npm/d3@7.9.0/+esm";
import maplibregl from "https://cdn.jsdelivr.net/npm/maplibre-gl@4.7.1/+esm";
import { State } from "./state.js";
import { MapView } from "./map_view.js";
import { StreetViewPopup } from "./street_view.js";
import { distToLonLat } from "./projection.js";

// Bus icon, source-coloured: High-Freq green, Low-Freq yellow.
function busElement(fill) {
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

const $ = (id) => document.getElementById(id);

// linear interp on ascending xs
function interp(xs, ys, xq) {
  if (xq <= xs[0]) return ys[0];
  if (xq >= xs[xs.length - 1]) return ys[ys.length - 1];
  let lo = 0, hi = xs.length - 1;
  while (hi - lo > 1) { const m = (lo + hi) >> 1; if (xs[m] <= xq) lo = m; else hi = m; }
  const f = (xq - xs[lo]) / (xs[hi] - xs[lo] || 1);
  return ys[lo] + f * (ys[hi] - ys[lo]);
}

const CAT_COLOR = {
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
const color = (c) => CAT_COLOR[c] || "#999";

const S = {
  trip: null,
  tab: "trajectory",
  showFull: false, // false = crop both tabs to the phone-GPS window
  speedX: "time",  // "time" | "distance" for the speed tab x-axis
  // Persisted visible domains per tab (null = default extent). Survive resize.
  view: { trajectory: null, speed: null },
  toggles: {
    phoneCurve: true, phoneRaw: false, r2Curve: true, r2Raw: false, stops: false,
    phoneSpeed: true, r2Speed: true, dAVL: true, dWeb: true, dPhone: true, dR2: true,
    busHi: true, busLo: true,
  },
  // map (speed tab): pub/sub + ported views, rebuilt per trip
  mapState: null, mapView: null, streetView: null,
  busHi: null, busLo: null,     // maplibre markers (High-Freq green / Low-Freq yellow)
  speedCursor: null,            // {showDist, hide} for the current speed svg
  lastV: null,                  // last hovered x-value (current speed units) — buses persist here
  tToDist: null, distToT: null, // High-Freq (phone) time<->distance
  r2ToDist: null,               // Low-Freq (R2) time->distance
};

// Position the bus marker(s) for an x-value in the current speed units.
function placeBusesAt(v) {
  if (!S.busLo || v == null) return;
  if (S.speedX === "distance") { // distance is the shared invariant -> one bus
    placeBus(S.busLo, v, true);
    placeBus(S.busHi, null, false);
  } else {
    placeBus(S.busHi, S.tToDist ? S.tToDist(v) : null, S.toggles.busHi);
    placeBus(S.busLo, S.r2ToDist ? S.r2ToDist(v) : null, S.toggles.busLo && !!S.trip.r2);
  }
}

// Pan (not zoom) the map to center a route distance.
function panMapTo(distM) {
  if (!S.mapView || distM == null || Number.isNaN(distM)) return;
  S.mapView.map.panTo(
    distToLonLat(distM, S.trip.shape.polyline_lonlat, S.trip.shape.cumdist_m),
    { duration: 500 });
}

// Place a bus marker at a route distance, or hide it.
function placeBus(marker, distM, show) {
  if (!marker) return;
  const el = marker.getElement();
  if (!show || distM == null || Number.isNaN(distM)) { el.style.display = "none"; return; }
  marker.setLngLat(distToLonLat(distM, S.trip.shape.polyline_lonlat, S.trip.shape.cumdist_m));
  el.style.display = "";
}

function fmtClock(tSec) {
  const d = new Date(S.trip.t0_epoch_ms + tSec * 1000);
  return d.toLocaleTimeString("en-US", { hour12: false, timeZone: "America/Chicago" });
}

function timeExtent() {
  const t = S.trip;
  let lo = Infinity, hi = -Infinity;
  const span = (arr) => arr.forEach((v) => { if (v != null) { lo = Math.min(lo, v); hi = Math.max(hi, v); } });
  for (const src of [t.phone, t.r2]) {
    if (!src) continue;
    span([src.curve.t[0], src.curve.t.at(-1)]);
    src.delays.forEach((d) => span([d.t_start, d.t_end]));
  }
  t.webapp_delays.forEach((d) => span([d.t_start, d.t_end ?? d.t_start + 5]));
  if (!isFinite(lo)) { lo = 0; hi = 1; }
  return [lo, hi];
}

function pad([lo, hi], frac = 0.03) {
  const d = (hi - lo) * frac || 1;
  return [lo - d, hi + d];
}

// Default x-extent: the phone-GPS window (cropped) unless "show full trip" is on.
function defaultXExtent() {
  if (S.showFull) return timeExtent();
  const c = S.trip.phone.curve;
  return pad([c.t[0], c.t.at(-1)], 0.02);
}

// Default x-extent (meters) for the speed tab in distance mode.
function defaultDistExtentM() {
  const t = S.trip;
  if (S.showFull) {
    let m = 0;
    for (const s of [t.phone, t.r2]) if (s) m = Math.max(m, d3.max(s.curve.dist_m));
    return [0, m];
  }
  const dm = t.phone.curve.dist_m;
  return pad([d3.min(dm), d3.max(dm)], 0.02);
}

// Default y-extent (km) for the trajectory tab: phone's distance span when
// cropped, the whole route when showing the full trip.
function defaultYExtentKm() {
  const t = S.trip;
  if (S.showFull) {
    let maxD = 0;
    for (const s of [t.phone, t.r2]) if (s) maxD = Math.max(maxD, d3.max(s.curve.dist_m));
    return [0, maxD / 1000];
  }
  const dm = t.phone.curve.dist_m;
  return pad([d3.min(dm) / 1000, d3.max(dm) / 1000], 0.04);
}

// ------------------------------------------------------- pan/zoom plumbing

function clampDom([lo, hi], full) {
  const fspan = full[1] - full[0];
  if (hi - lo >= fspan) return full.slice();
  if (lo < full[0]) { hi += full[0] - lo; lo = full[0]; }
  if (hi > full[1]) { lo -= hi - full[1]; hi = full[1]; }
  return [lo, hi];
}

function zoomAxis(scale, dom, full, pixel, factor) {
  const v = scale.invert(pixel);
  const minSpan = (full[1] - full[0]) / 600;
  let lo = v - (v - dom[0]) * factor;
  let hi = v + (dom[1] - v) * factor;
  if (hi - lo < minSpan) { const m = (lo + hi) / 2; lo = m - minSpan / 2; hi = m + minSpan / 2; }
  return clampDom([lo, hi], full);
}

// state = { x:{scale,full}, y:{scale,full}|null }, store = the per-tab view obj
// to mutate, axisMode(e)->{x,y}, panY bool, redraw()
function installInteraction(svg, state, store, axisMode, panY, redraw) {
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

function makeSvg() {
  $("chart").innerHTML = "";
  const width = $("chart").clientWidth || 900;
  const height = Math.max(260, $("chart").clientHeight || 460);
  const svg = d3.select("#chart").append("svg")
    .attr("width", width).attr("height", height)
    .attr("viewBox", `0 0 ${width} ${height}`)
    .style("user-select", "none").style("display", "block");
  return { svg, width, height };
}

// ---------------------------------------------------------- trajectory tab

function renderTrajectory() {
  const t = S.trip;
  const M = { l: 56, r: 16, t: 14, b: 38 };
  const { svg, width, height } = makeSvg();

  const xFull = defaultXExtent();
  const yFull = defaultYExtentKm();
  const view = S.view.trajectory || (S.view.trajectory = { x: xFull.slice(), y: yFull.slice() });

  const x = d3.scaleLinear().range([M.l, width - M.r]);
  const y = d3.scaleLinear().range([height - M.b, M.t]);

  svg.append("text").attr("transform", "rotate(-90)").attr("x", -(height / 2)).attr("y", 14)
    .attr("text-anchor", "middle").attr("font-size", 12).attr("fill", "#555")
    .text("distance along route (km)");

  const gGrid = svg.append("g").attr("class", "grid");
  const gAxisY = svg.append("g").attr("class", "axis").attr("transform", `translate(${M.l},0)`);
  const gStops = svg.append("g");
  const gPhone = svg.append("g");
  const gR2 = svg.append("g");
  const gAxisX = svg.append("g").attr("class", "axis").attr("transform", `translate(0,${height - M.b})`);
  // clip so panned content doesn't spill over the axes
  svg.append("clipPath").attr("id", "clip-traj").append("rect")
    .attr("x", M.l).attr("y", M.t).attr("width", width - M.l - M.r).attr("height", height - M.t - M.b);
  for (const g of [gStops, gPhone, gR2]) g.attr("clip-path", "url(#clip-traj)");

  function redraw() {
    x.domain(view.x); y.domain(view.y);
    gAxisX.call(d3.axisBottom(x).ticks(8).tickFormat(fmtClock));
    gAxisY.call(d3.axisLeft(y).ticks(8));
    gGrid.attr("transform", `translate(0,${height - M.b})`)
      .call(d3.axisBottom(x).ticks(8).tickSize(-(height - M.t - M.b)).tickFormat(""));
    gGrid.selectAll(".domain").remove();

    gStops.selectAll("line").remove();
    if (S.toggles.stops) {
      gStops.selectAll("line").data(t.features.filter((f) => f.kind === "stop"))
        .join("line").attr("class", "stopline")
        .attr("x1", M.l).attr("x2", width - M.r)
        .attr("y1", (d) => y(d.dist_m / 1000)).attr("y2", (d) => y(d.dist_m / 1000));
    }
    drawSource(gPhone, t.phone, "phone", S.toggles.phoneCurve, S.toggles.phoneRaw);
    drawSource(gR2, t.r2, "r2", S.toggles.r2Curve, S.toggles.r2Raw);
  }

  function drawSource(g, src, cls, showCurve, showRaw) {
    g.selectAll("*").remove();
    if (!src) return;
    if (showRaw) {
      g.selectAll("circle").data(src.raw_pings).join("circle").attr("class", `raw ${cls}`)
        .attr("cx", (d) => x(d.t)).attr("cy", (d) => y(d.dist_m / 1000)).attr("r", 2).attr("opacity", 0.45);
    }
    if (showCurve) {
      const line = d3.line().x((d) => x(d[0])).y((d) => y(d[1]));
      const pts = src.curve.t.map((tt, i) => [tt, src.curve.dist_m[i] / 1000]);
      g.append("path").attr("class", `curve ${cls}`).attr("d", line(pts));
    }
  }

  x.domain(view.x); y.domain(view.y);
  installInteraction(svg,
    { x: { scale: x, full: xFull }, y: { scale: y, full: yFull } },
    view,
    (e) => ({ x: !e.shiftKey, y: !(e.metaKey || e.ctrlKey) }), // shift=y, cmd=x, none=both
    true, redraw);
  redraw();
}

// --------------------------------------------------------------- speed tab

function renderSpeed() {
  const t = S.trip;
  const distMode = S.speedX === "distance";
  const M = { l: 56, r: 16, t: 14 };
  const { svg, width, height } = makeSvg();
  const rowH = 26, rowGap = 6;
  // AVL covers the whole trip; in distance mode keep only stops the observed
  // trajectory can place (within its time span), since pre-boarding stops have
  // no route distance. In time mode show them all (full-trip view reveals them).
  const pT = t.phone.curve.t;
  const avlRows = distMode
    ? (t.avl_delays || []).filter((b) => b.t_start >= pT[0] && b.t_start <= pT[pT.length - 1])
    : (t.avl_delays || []);
  const rows = [
    { key: "dAVL", label: "AVL", delays: avlRows, src: t.phone.curve, avl: true },
    { key: "dWeb", label: "Observed", delays: t.webapp_delays, src: t.phone.curve },
    { key: "dPhone", label: "High-Freq", delays: t.phone ? t.phone.delays : [], src: t.phone.curve },
    { key: "dR2", label: "Low-Freq", delays: t.r2 ? t.r2.delays : [], src: t.r2 ? t.r2.curve : null },
  ];
  const stripH = rows.length * (rowH + rowGap);
  const axisY = height - 24;
  const speedH = axisY - stripH - 12;
  const rowTop = speedH + 12;

  let maxV = 5;
  for (const s of [t.phone, t.r2]) if (s) maxV = Math.max(maxV, d3.max(s.curve.speed_mph));
  const xFull = distMode ? defaultDistExtentM() : defaultXExtent();
  const view = S.view.speed || (S.view.speed = { x: xFull.slice() });
  const fmtX = distMode ? (v) => (v / 1000).toFixed(1) : fmtClock;
  // Map a delay's time onto the current x-axis (distance via its own source).
  const delayX = (row, tSec) => (distMode ? interp(row.src.t, row.src.dist_m, tSec) : tSec);

  const x = d3.scaleLinear().range([M.l, width - M.r]);
  const y = d3.scaleLinear().domain([0, maxV]).nice().range([speedH, M.t]);

  svg.append("g").attr("class", "axis").attr("transform", `translate(${M.l},0)`).call(d3.axisLeft(y));
  svg.append("text").attr("transform", "rotate(-90)").attr("x", -(speedH / 2)).attr("y", 14)
    .attr("text-anchor", "middle").attr("font-size", 12).attr("fill", "#555").text("speed (mph)");
  svg.append("text").attr("x", (M.l + width - M.r) / 2).attr("y", height - 4)
    .attr("text-anchor", "middle").attr("font-size", 11).attr("fill", "#888")
    .text(distMode ? "distance along route (km)" : "time (Chicago)");
  rows.forEach((r, i) => {
    const ry = rowTop + i * (rowH + rowGap);
    svg.append("text").attr("class", "rowlabel").attr("x", M.l - 6).attr("y", ry + rowH / 2 + 3)
      .attr("text-anchor", "end").text(r.label);
  });

  const gGrid = svg.append("g").attr("class", "grid");
  const gPhone = svg.append("g");
  const gR2 = svg.append("g");
  const gRows = rows.map(() => svg.append("g"));
  const gAxisX = svg.append("g").attr("class", "axis").attr("transform", `translate(0,${axisY})`);
  svg.append("clipPath").attr("id", "clip-spd").append("rect")
    .attr("x", M.l).attr("y", M.t).attr("width", width - M.l - M.r).attr("height", axisY - M.t);
  for (const g of [gPhone, gR2, ...gRows]) g.attr("clip-path", "url(#clip-spd)");

  // 5 mph slowdown-detection threshold reference line (static; y is fixed).
  svg.append("line").attr("class", "threshold-line")
    .attr("x1", M.l).attr("x2", width - M.r).attr("y1", y(5)).attr("y2", y(5));
  svg.append("text").attr("class", "threshold-label")
    .attr("x", width - M.r - 2).attr("y", y(5) - 3).attr("text-anchor", "end").text("5 mph");

  function redraw() {
    x.domain(view.x);
    gAxisX.call(d3.axisBottom(x).ticks(8).tickFormat(fmtX));
    gGrid.attr("transform", `translate(0,${axisY})`)
      .call(d3.axisBottom(x).ticks(8).tickSize(-(axisY - M.t)).tickFormat(""));
    gGrid.selectAll(".domain").remove();

    drawSpeed(gPhone, t.phone, "phone", S.toggles.phoneSpeed);
    drawSpeed(gR2, t.r2, "r2", S.toggles.r2Speed);

    rows.forEach((r, i) => {
      const ry = rowTop + i * (rowH + rowGap);
      const g = gRows[i]; g.selectAll("*").remove();
      const on = S.toggles[r.key];
      g.append("rect").attr("x", M.l).attr("y", ry).attr("width", width - M.r - M.l)
        .attr("height", rowH).attr("fill", on ? "#fcfcfd" : "#f3f3f5").attr("stroke", "#eee");
      if (!on || (distMode && !r.src)) return;
      const x0 = (d) => x(delayX(r, d.t_start));
      const x1 = (d) => x(delayX(r, d.t_end ?? d.t_start + 5));
      const sel = g.selectAll("g.bar").data(r.delays.filter((d) => d.t_start != null))
        .join("g").attr("class", "bar");
      sel.append("rect").attr("class", "delaybar")
        .attr("x", x0).attr("y", ry + 2)
        .attr("width", (d) => Math.max(r.avl ? 2.5 : 1.5, x1(d) - x0(d)))
        .attr("height", rowH - 4).attr("fill", (d) => color(d.category))
        .on("mousemove", (e, d) => (r.avl ? showAvlTip(e, d) : showTip(e, r.label, d)))
        .on("mouseleave", hideTip);
      sel.append("text").attr("class", "delaytext")
        .attr("x", (d) => x0(d) + 4).attr("y", ry + rowH / 2 + 3)
        .text((d) => {
          const w = x1(d) - x0(d);
          const lab = r.avl && d.category === "avl_other" ? d.event_desc : (d.label || d.category);
          return w > 26 ? lab.slice(0, Math.floor(w / 6)) : "";
        });
    });
    if (S.lastV != null) showCursorVal(S.lastV); // keep cursor aligned through zoom/pan
  }

  function drawSpeed(g, src, cls, show) {
    g.selectAll("*").remove();
    if (!src || !show) return;
    const line = d3.line().x((d) => x(d[0])).y((d) => y(d[1]));
    const pts = src.curve.t.map((tt, i) => [distMode ? src.curve.dist_m[i] : tt, src.curve.speed_mph[i]]);
    g.append("path").attr("class", `curve ${cls}`).attr("d", line(pts));
  }

  // Vertical cursor (in current x units) that drives the map bus icon(s).
  const cursorLine = svg.append("line").attr("class", "chart-cursor")
    .attr("y1", M.t).attr("y2", axisY).style("display", "none");
  const showCursorVal = (v) => {
    const px = x(v);
    if (px < M.l || px > width - M.r) { cursorLine.style("display", "none"); return; }
    cursorLine.attr("x1", px).attr("x2", px).style("display", null);
  };
  S.speedCursor = {
    // map publishes a route distance; convert to this axis's units
    showDist: (dM) => showCursorVal(distMode ? dM : S.distToT(dM)),
    hide: () => cursorLine.style("display", "none"),
  };

  const node = svg.node();
  const toDistM = (v) => (distMode ? v : S.tToDist(v));
  // Hover: live preview of cursor + bus(es). They PERSIST after the cursor
  // leaves the chart (no mouseleave-hide), so you can study the map.
  svg.on("mousemove.cur", (e) => {
    const [px] = d3.pointer(e, node);
    if (px < M.l || px > width - M.r) return;
    S.lastV = x.invert(px);
    showCursorVal(S.lastV);
    placeBusesAt(S.lastV);
  });
  // Single click -> pan map to that location (keep zoom). Double click ->
  // Street View. A timer disambiguates the two.
  let clickTimer = null;
  svg.on("click.cur", (e) => {
    if (node._dragMoved) return; // was a pan-drag, not a click
    if (clickTimer) { clearTimeout(clickTimer); clickTimer = null; return; } // 2nd click
    const [px] = d3.pointer(e, node);
    if (px < M.l || px > width - M.r) return;
    const v = x.invert(px);
    clickTimer = setTimeout(() => { clickTimer = null; panMapTo(toDistM(v)); }, 250);
  });
  svg.on("dblclick.cur", (e) => {
    if (clickTimer) { clearTimeout(clickTimer); clickTimer = null; }
    const [px] = d3.pointer(e, node);
    if (px < M.l || px > width - M.r) return;
    S.mapState?.publish("streetview:open", { distM: toDistM(x.invert(px)) });
  });

  x.domain(view.x);
  installInteraction(svg,
    { x: { scale: x, full: xFull }, y: null },
    view,
    () => ({ x: true, y: false }), // speed: horizontal only
    false, redraw);
  redraw();
  // Restore the persisted cursor + buses after a (re)render.
  if (S.lastV != null) { showCursorVal(S.lastV); placeBusesAt(S.lastV); }
}

function showTip(e, rowLabel, d) {
  const dur = d.t_end != null ? `${Math.round(d.t_end - d.t_start)}s` : "open";
  $("tooltip").innerHTML =
    `<b>${rowLabel}</b> · ${d.category}<br>${d.label || "—"}<br>` +
    `${fmtClock(d.t_start)}${d.t_end != null ? "–" + fmtClock(d.t_end) : ""} (${dur})`;
  $("tooltip").classList.remove("hidden");
  $("tooltip").style.left = e.clientX + 12 + "px";
  $("tooltip").style.top = e.clientY + 12 + "px";
}
function hideTip() { $("tooltip").classList.add("hidden"); }

// Rich AVL stop tooltip: dwell + passenger load before/after + door flows.
function showAvlTip(e, d) {
  const head =
    `<b>AVL · ${d.event_desc}</b> (event ${d.event_type})<br>` +
    `${d.label}${d.stop_seq != null ? " · seq " + d.stop_seq : ""}<br>` +
    `${fmtClock(d.t_start)}–${fmtClock(d.t_end)} · dwell ${d.dwell_s}s`;
  let pax;
  if (d.flow > 0) {
    pax =
      `Load: ${d.load_before ?? "?"} → ${d.load_after ?? "?"}<br>` +
      `On ${d.on_total} (front ${d.on_front} / rear ${d.on_rear}) · ` +
      `Off ${d.off_total} (front ${d.off_front} / rear ${d.off_rear})<br>` +
      `Flow ${d.flow}` +
      (d.dwell_per_pax != null ? ` · ${d.dwell_per_pax}s / passenger` : "");
  } else {
    pax = `Load: ${d.load_after ?? "?"} · no passenger activity`;
  }
  $("tooltip").innerHTML = head + "<hr style='border:none;border-top:1px solid #555;margin:4px 0'>" + pax;
  $("tooltip").classList.remove("hidden");
  $("tooltip").style.left = e.clientX + 12 + "px";
  $("tooltip").style.top = e.clientY + 12 + "px";
}

// ----------------------------------------------------------------- render

// ----------------------------------------------------------- map lifecycle

function ensureMap() {
  if (S.mapView || !S.trip || !S.trip.shape) return;
  S.mapState = new State();
  S.streetView = new StreetViewPopup(S.trip, S.mapState);
  S.mapView = new MapView($("map"), S.trip, S.mapState);
  // Two source-coloured buses, driven by the speed-chart cursor.
  S.busHi = new maplibregl.Marker({ element: busElement("#2e9e4f"),
    rotationAlignment: "viewport", pitchAlignment: "viewport", offset: [0, -16], anchor: "bottom" })
    .setLngLat(S.trip.shape.polyline_lonlat[0]).addTo(S.mapView.map);
  S.busLo = new maplibregl.Marker({ element: busElement("#f4b400"),
    rotationAlignment: "viewport", pitchAlignment: "viewport", offset: [0, -16], anchor: "bottom" })
    .setLngLat(S.trip.shape.polyline_lonlat[0]).addTo(S.mapView.map);
  // map hover -> chart cursor preview (the cursor + buses otherwise persist)
  S.mapState.subscribe("dist:hovered", (e) => { if (e.source === "map") S.speedCursor?.showDist(e.distM); });
  setTimeout(() => S.mapView?.resize(), 60);
}

function teardownMap() {
  S.mapView?.destroy();        // map.remove() also drops the bus markers
  S.streetView?.destroy();
  S.mapView = S.streetView = S.mapState = S.busHi = S.busLo = null;
}

function render() {
  $("controls").querySelectorAll(".ctrl-group").forEach((g) =>
    g.classList.toggle("hidden", g.dataset.for !== S.tab));
  const isSpeed = S.tab === "speed";
  document.body.classList.toggle("show-map", isSpeed);
  if (!S.trip) return;
  if (isSpeed) renderSpeed(); else renderTrajectory();
  if (isSpeed) { ensureMap(); setTimeout(() => S.mapView?.resize(), 60); }
}

async function loadTrip(key) {
  teardownMap();
  S.trip = await fetch(`./data/${key}.json`, { cache: "no-store" }).then((r) => r.json());
  S.view = { trajectory: null, speed: null };
  S.lastV = null;
  const t = S.trip;
  // Trajectory time<->distance (dist_m monotonic), for cursor/buses/street view.
  const c = t.phone.curve;
  S.tToDist = (tSec) => interp(c.t, c.dist_m, tSec);
  S.distToT = (dM) => interp(c.dist_m, c.t, dM);
  S.r2ToDist = t.r2 ? ((tSec) => interp(t.r2.curve.t, t.r2.curve.dist_m, tSec)) : null;
  const r2n = t.r2 ? `${t.r2.n_on_route}/${t.r2.n_pings}` : "none";
  $("trip-meta").textContent =
    `trip ${t.trip_id} · bus #${t.bus_id} · pattern ${t.pattern_id} · ` +
    `High-Freq ${t.phone.n_on_route}/${t.phone.n_pings} on-route · Low-Freq ${r2n} · observer ${t.observer || "—"}`;
  render();
}

async function init() {
  const idx = await fetch("./data/index.json", { cache: "no-store" }).then((r) => r.json());
  const sel = $("trip-select");
  for (const tr of idx.trips) {
    const o = document.createElement("option");
    o.value = tr.key; o.textContent = tr.label;
    sel.appendChild(o);
  }
  sel.onchange = () => loadTrip(sel.value);

  document.querySelectorAll("#tabs button").forEach((b) =>
    b.onclick = () => {
      document.querySelectorAll("#tabs button").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      S.tab = b.dataset.tab;
      render();
    });

  const bind = (id, key) => { $(id).onchange = (e) => { S.toggles[key] = e.target.checked; render(); }; };
  bind("t-phoneCurve", "phoneCurve"); bind("t-phoneRaw", "phoneRaw");
  bind("t-r2Curve", "r2Curve"); bind("t-r2Raw", "r2Raw"); bind("t-stops", "stops");
  bind("s-phoneSpeed", "phoneSpeed"); bind("s-r2Speed", "r2Speed");
  bind("s-dAVL", "dAVL"); bind("s-dWeb", "dWeb"); bind("s-dPhone", "dPhone"); bind("s-dR2", "dR2");
  bind("s-busHi", "busHi"); bind("s-busLo", "busLo");
  document.querySelectorAll('input[name="speedx"]').forEach((r) =>
    r.onchange = (e) => {
      if (!e.target.checked) return;
      S.speedX = e.target.value;
      S.view.speed = null; // units changed -> recompute default extent
      S.lastV = null;      // x-value units changed -> drop persisted cursor
      render();
    });
  // M / S basemap shortcuts (speed tab, when the map exists)
  document.addEventListener("keydown", (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey || !S.mapView) return;
    const tag = (e.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select") return;
    const k = e.key.toLowerCase();
    if (k === "m") S.mapState.publish("basemap:changed", { value: "map" });
    else if (k === "s") S.mapState.publish("basemap:changed", { value: "satellite" });
  });

  $("reset-zoom").onclick = () => { S.view[S.tab] = null; render(); };
  $("show-full").onchange = (e) => {
    S.showFull = e.target.checked;
    S.view = { trajectory: null, speed: null }; // re-crop both tabs to new extent
    render();
  };
  window.addEventListener("resize", render);

  if (idx.trips.length) { sel.value = idx.trips[0].key; await loadTrip(idx.trips[0].key); }
}

init().catch((err) => {
  console.error(err);
  $("chart").innerHTML = `<pre style="color:#c00;padding:16px">${err.stack || err}</pre>`;
});
