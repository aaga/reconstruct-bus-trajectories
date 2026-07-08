// Merged trip-comparison dashboard — bootstrap. Owns the shared state S, the
// map lifecycle, and the tab/control wiring; delegates rendering to the
// TrajectoryView and SpeedView classes (State-shared, so behaviour matches the
// original monolithic main.js exactly).
//
// Interaction:
//   trajectory — wheel zooms both axes; SHIFT = vertical only,
//                CMD/CTRL = horizontal only; drag pans.
//   speed      — wheel/drag are horizontal only (time).

import maplibregl from "https://cdn.jsdelivr.net/npm/maplibre-gl@4.7.1/+esm";
import { State } from "./state.js";
import { MapView } from "./map_view.js";
import { StreetViewPopup } from "./street_view.js";
import { $, busElement, interp, getSource } from "./chart_util.js";
import { TrajectoryView } from "./views/trajectory_view.js";
import { SpeedView } from "./views/speed_view.js";
import { OverallDelayView } from "./views/overall_delay_view.js";
import { DelayView } from "./views/delay_view.js";

const S = {
  main: "single",          // "single" | "average"
  trip: null,
  agg: null,               // loaded aggregate payload (average trip)
  tab: "trajectory",       // single-trip sub-tab: "trajectory" | "speed"
  atab: "overall",         // average-trip sub-tab: "overall" | "segment"
  showFull: false, // false = crop both tabs to the phone-GPS window
  speedX: "time",  // "time" | "distance" for the speed tab x-axis
  // Persisted visible domains per tab (null = default extent). Survive resize.
  view: { trajectory: null, speed: null },
  toggles: {
    phoneCurve: true, phoneRaw: false, r2Curve: true, r2Raw: false, stops: false,
    phoneSpeed: true, r2Speed: true, dAVL: true, dWeb: true,
  },
  // map (speed tab): pub/sub + ported views, rebuilt per trip
  mapState: null, mapView: null, streetView: null,
  busHi: null, busLo: null,     // maplibre markers (High-Freq green / Low-Freq yellow)
  speedCursor: null,            // { render(spec), hide() } for the current speed svg
  cursor: null,                 // persisted cursor: {kind:"chart",v} | {kind:"map",distM}
  tToDist: null, distToT: null, // High-Freq (phone) time<->distance
  r2ToDist: null,               // Low-Freq (R2) time->distance
};

const trajView = new TrajectoryView(S);
const speedView = new SpeedView(S);
const overallView = new OverallDelayView(S);

// Display modes. Rich = every source + delay row; Lite = the primary source +
// its inferred delays only (emulating the single-trip route dashboard). Modes
// are just preset toggle states over the shared views — no separate chart code.
const MODE_PRESETS = {
  rich: { phoneCurve: true, phoneRaw: false, r2Curve: true, r2Raw: false, stops: false,
          phoneSpeed: true, r2Speed: true, dAVL: true, dWeb: true },
  lite: { phoneCurve: true, phoneRaw: false, r2Curve: false, r2Raw: false, stops: false,
          phoneSpeed: true, r2Speed: false, dAVL: false, dWeb: false },
};
const TOGGLE_IDS = {
  "t-phoneCurve": "phoneCurve", "t-phoneRaw": "phoneRaw", "t-r2Curve": "r2Curve", "t-r2Raw": "r2Raw", "t-stops": "stops",
  "s-phoneSpeed": "phoneSpeed", "s-r2Speed": "r2Speed", "s-dAVL": "dAVL", "s-dWeb": "dWeb",
};

function setMode(mode) {
  S.mode = mode;
  Object.assign(S.toggles, MODE_PRESETS[mode]);
  for (const [id, key] of Object.entries(TOGGLE_IDS)) { const el = $(id); if (el) el.checked = S.toggles[key]; }
  document.querySelectorAll("#modes button").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
  render();
}

// ----------------------------------------------------------- map lifecycle

// The map/street-view share a per-trip State that lives as long as the trip
// (created on load, not on first map build) — so double-click → Street View
// works on the trajectory tab too, where there is no map.
function ensureTripState() {
  if (!S.trip || S.mapState) return;
  S.mapState = new State();
  S.streetView = new StreetViewPopup(S.trip, S.mapState);
}

function ensureMap() {
  if (S.mapView || !S.trip || !S.trip.shape) return;
  ensureTripState();
  S.mapView = new MapView($("map"), S.trip, S.mapState);
  // Two source-coloured buses, driven by the speed-chart cursor. Low-Freq is
  // added first so the High-Freq bus (added last) renders in front; both are
  // 75% opaque (CSS) so an overlapping bus shows through.
  S.busLo = new maplibregl.Marker({ element: busElement("#c026d3"),
    rotationAlignment: "viewport", pitchAlignment: "viewport", offset: [0, 0], anchor: "bottom" })
    .setLngLat(S.trip.shape.polyline_lonlat[0]).addTo(S.mapView.map);
  S.busHi = new maplibregl.Marker({ element: busElement("#52c41a"),
    rotationAlignment: "viewport", pitchAlignment: "viewport", offset: [0, 0], anchor: "bottom" })
    .setLngLat(S.trip.shape.polyline_lonlat[0]).addTo(S.mapView.map);
  // map hover -> chart cursor preview (the cursor + buses otherwise persist)
  S.mapState.subscribe("dist:hovered", (e) => {
    if (e.source !== "map") return;
    S.cursor = { kind: "map", distM: e.distM };
    S.speedCursor?.render(S.cursor);
  });
  // map pan/zoom -> chart x-range (converting distance→time when in time mode)
  S.mapState.subscribe("range:changed", (e) => {
    if (e.source !== "map" || S.tab !== "speed") return;
    const distMode = S.speedX === "distance";
    let [lo, hi] = e.visibleDistRangeM;
    let a = distMode ? lo : S.distToT(lo);
    let b = distMode ? hi : S.distToT(hi);
    if (a > b) [a, b] = [b, a];
    S._applyingMapRange = true;
    S.view.speed = { x: [a, b] };
    render();
    S._applyingMapRange = false;
  });
  // Once the map has first rendered (idle → canvas correctly sized), resize and
  // fit it to the chart's current range, so a trip is framed on load without
  // needing a manual pan first.
  S.mapView.map.once("idle", () => { S.mapView?.resize(); publishChartRange(); });
  setTimeout(() => S.mapView?.resize(), 60);
}

// Push the speed chart's visible route-distance range to the map (used to sync
// the map to the chart on map load; ongoing sync happens inside SpeedView.redraw).
function publishChartRange() {
  if (!S.mapState || S._applyingMapRange || S.main !== "single" || S.tab !== "speed") return;
  const vx = S.view.speed?.x;
  if (!vx) return;
  const distMode = S.speedX === "distance";
  let a = distMode ? vx[0] : S.tToDist(vx[0]);
  let b = distMode ? vx[1] : S.tToDist(vx[1]);
  if (a > b) [a, b] = [b, a];
  S.mapState.publish("range:changed", { visibleDistRangeM: [a, b], source: "chart" });
}

function teardownMap() {
  S.mapView?.destroy();        // map.remove() also drops the bus markers
  S.streetView?.destroy();
  S.mapView = S.streetView = S.mapState = S.busHi = S.busLo = null;
}

function render() {
  if (S.main === "average") { renderAverage(); return; }
  renderSingle();
}

function renderSingle() {
  teardownAggViews(); // clean up the average-trip map/DelayView if we came from it
  ensureTripState();  // map/street-view State available on both tabs (dbl-click → Street View)
  $("controls").querySelectorAll(".ctrl-group").forEach((g) =>
    g.classList.toggle("hidden", g.dataset.for !== S.tab));
  const isSpeed = S.tab === "speed";
  document.body.classList.toggle("show-map", isSpeed);
  if (!S.trip) return;
  if (isSpeed) speedView.render(); else trajView.render();
  // Map-side effects (create/resize the map, re-sync its range) only when the
  // chart or a tab switch drives the render. When the MAP is driving (a pan/zoom
  // gesture), we just re-render the chart — calling resize() mid-drag would
  // interrupt the map's own drag gesture.
  if (isSpeed && !S._applyingMapRange) {
    ensureMap();
    setTimeout(() => S.mapView?.resize(), 60);
    publishChartRange();
  }
}

// Average-trip: Overall delay (F3 breakdown) or Delay per segment (DelayView
// Segments/Stems mirrored to the shared map, coupled via a per-aggregate State).
function renderAverage() {
  teardownMap(); // tear down any single-trip map/buses
  if (!S.agg) { teardownAggViews(); document.body.classList.remove("show-map"); $("chart").innerHTML = ""; return; }
  if (S.atab === "segment") {
    document.body.classList.add("show-map");
    teardownAggViews();
    ensureAggViews();
  } else {
    teardownAggViews();
    document.body.classList.remove("show-map");
    overallView.render();
  }
}

function ensureAggViews() {
  if (S.delayView) return;
  $("chart").innerHTML = "";
  S.aggState = new State();
  S.delayView = new DelayView($("chart"), S.agg, S.aggState);
  S.aggMap = new MapView($("map"), S.agg, S.aggState);
  S.aggStreet = new StreetViewPopup(S.agg, S.aggState);
  S.aggMap.map.once("idle", () => S.aggMap?.resize());
  setTimeout(() => S.aggMap?.resize(), 60);
}

function teardownAggViews() {
  S.aggMap?.destroy();
  S.aggStreet?.destroy();
  if (S.delayView) $("chart").innerHTML = ""; // DelayView has no destroy()
  S.aggMap = S.aggStreet = S.aggState = S.delayView = null;
}

async function loadTrip(key) {
  teardownMap();
  S.trip = await fetch(`../data/${key}.json`, { cache: "no-store" }).then((r) => r.json());
  S.view = { trajectory: null, speed: null };
  S.cursor = null;
  const t = S.trip;
  const phone = getSource(t, "phone");
  const r2 = getSource(t, "r2");
  // Trajectory time<->distance (dist_m monotonic), for cursor/buses/street view.
  const c = phone.curve;
  S.tToDist = (tSec) => interp(c.t, c.dist_m, tSec);
  S.distToT = (dM) => interp(c.dist_m, c.t, dM);
  S.r2ToDist = r2 ? ((tSec) => interp(r2.curve.t, r2.curve.dist_m, tSec)) : null;
  const r2n = r2 ? `${r2.n_on_route}/${r2.n_pings}` : "none";
  $("trip-meta").textContent =
    `trip ${t.trip_id} · bus #${t.bus_id} · pattern ${t.pattern_id} · ` +
    `High-Freq ${phone.n_on_route}/${phone.n_pings} on-route · Low-Freq ${r2n} · observer ${t.observer || "—"}`;
  render();
}

async function loadAggregate(key) {
  teardownMap();
  S.agg = await fetch(`../data/${key}.json`, { cache: "no-store" }).then((r) => r.json());
  $("trip-meta").textContent = `${S.agg.label} · ${S.agg.n_trips} trips`;
  render();
}

// Switch main tab: toggle the per-main selector bars, sub-tabs, and controls.
function setMain(main) {
  S.main = main;
  document.querySelectorAll("#main-tabs button").forEach((b) => b.classList.toggle("active", b.dataset.main === main));
  for (const el of document.querySelectorAll(".mainbar, .subtabs, #controls")) {
    el.classList.toggle("hidden", el.dataset.main !== main);
  }
  $("trip-meta").textContent = "";
  render();
}

async function init() {
  const idx = await fetch("../data/index.json", { cache: "no-store" }).then((r) => r.json());
  const items = idx.items;
  const trips = items.filter((i) => i.kind === "trip");
  const aggs = items.filter((i) => i.kind === "aggregate");

  const tripSel = $("trip-select");
  for (const it of trips) {
    const o = document.createElement("option");
    o.value = it.key; o.textContent = it.label;
    tripSel.appendChild(o);
  }
  tripSel.onchange = () => loadTrip(tripSel.value);

  const routeSel = $("route-select");
  for (const it of aggs) {
    const o = document.createElement("option");
    o.value = it.key; o.textContent = it.label;
    routeSel.appendChild(o);
  }
  routeSel.onchange = () => loadAggregate(routeSel.value);

  // Main tabs (single / average). First switch to average loads the aggregate.
  document.querySelectorAll("#main-tabs button").forEach((b) =>
    (b.onclick = () => {
      setMain(b.dataset.main);
      if (b.dataset.main === "average" && !S.agg && aggs.length) {
        routeSel.value = aggs[0].key; loadAggregate(aggs[0].key);
      }
    }));

  // Single-trip sub-tabs
  document.querySelectorAll("#tabs button").forEach((b) =>
    (b.onclick = () => {
      document.querySelectorAll("#tabs button").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      S.tab = b.dataset.tab; render();
    }));
  // Average-trip sub-tabs
  document.querySelectorAll("#atabs button").forEach((b) =>
    (b.onclick = () => {
      document.querySelectorAll("#atabs button").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      S.atab = b.dataset.atab; render();
    }));

  const bind = (id, key) => { $(id).onchange = (e) => { S.toggles[key] = e.target.checked; render(); }; };
  bind("t-phoneCurve", "phoneCurve"); bind("t-phoneRaw", "phoneRaw");
  bind("t-r2Curve", "r2Curve"); bind("t-r2Raw", "r2Raw"); bind("t-stops", "stops");
  bind("s-phoneSpeed", "phoneSpeed"); bind("s-r2Speed", "r2Speed");
  bind("s-dAVL", "dAVL"); bind("s-dWeb", "dWeb");
  document.querySelectorAll("#modes button").forEach((b) => (b.onclick = () => setMode(b.dataset.mode)));
  document.querySelectorAll('input[name="speedx"]').forEach((r) =>
    r.onchange = (e) => {
      if (!e.target.checked) return;
      S.speedX = e.target.value;
      S.view.speed = null; // units changed -> recompute default extent
      S.cursor = null;      // x-value units changed -> drop persisted cursor
      render();
    });
  // Keyboard shortcuts. These must ALWAYS fire (no focused-element guard) —
  // single-trip: M/S basemap, H hide-features (speed tab map); average-trip:
  // B/E Segments/Stems, H hide-features, M/S basemap.
  document.addEventListener("keydown", (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const k = e.key.toLowerCase();
    const hide = () => { S._hideUnattr = !S._hideUnattr; return S._hideUnattr; };
    if (S.main === "average" && S.aggState) {
      if (k === "b") S.aggState.publish("delayMode:changed", { value: "segments" });
      else if (k === "e") S.aggState.publish("delayMode:changed", { value: "stems" });
      else if (k === "h") S.aggState.publish("hideUnattributed:changed", { value: hide() });
      else if (k === "m") S.aggState.publish("basemap:changed", { value: "map" });
      else if (k === "s") S.aggState.publish("basemap:changed", { value: "satellite" });
      return;
    }
    if (!S.mapState) return;
    if (k === "m") S.mapState.publish("basemap:changed", { value: "map" });
    else if (k === "s") S.mapState.publish("basemap:changed", { value: "satellite" });
    else if (k === "h") S.mapState.publish("hideUnattributed:changed", { value: hide() });
  });

  $("reset-zoom").onclick = () => { S.view[S.tab] = null; render(); };
  $("show-full").onchange = (e) => {
    S.showFull = e.target.checked;
    S.view = { trajectory: null, speed: null }; // re-crop both tabs to new extent
    render();
  };
  window.addEventListener("resize", render);

  if (trips.length) { tripSel.value = trips[0].key; await loadTrip(trips[0].key); }
}

init().catch((err) => {
  console.error(err);
  $("chart").innerHTML = `<pre style="color:#c00;padding:16px">${err.stack || err}</pre>`;
});
