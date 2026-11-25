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
import { NetworkData, NETWORK_CITIES } from "./network_data.js";
import { NetworkView, METRICS } from "./views/network_view.js";
import { AreasView } from "./views/areas_view.js";
import { ExploreView } from "./views/explore_view.js";

const S = {
  main: "single",          // "single" | "network"
  trip: null,
  tab: "trajectory",       // single-trip sub-tab: "trajectory" | "speed"
  showFull: false, // false = crop both tabs to the phone-GPS window
  speedX: "time",  // "time" | "distance" for the speed tab x-axis
  // Persisted visible domains per tab (null = default extent). Survive resize.
  view: { trajectory: null, speed: null },
  toggles: {
    phoneCurve: true, phoneRaw: false, r2Curve: true, r2Raw: false, stops: false,
    phoneSpeed: true, r2Speed: true, dAVL: true, dWeb: true, dLoss: false,
  },
  // map (speed tab): pub/sub + ported views, rebuilt per trip
  mapState: null, mapView: null, streetView: null,
  busHi: null, busLo: null,     // maplibre markers (High-Freq green / Low-Freq yellow)
  speedCursor: null,            // { render(spec), hide() } for the current speed svg
  cursor: null,                 // persisted cursor: {kind:"chart",v} | {kind:"map",distM}
  tToDist: null, distToT: null, // High-Freq (phone) time<->distance
  r2ToDist: null,               // Low-Freq (R2) time->distance
  // Network tab (see views/network_view.js)
  ntab: "map",                  // "map" | "areas"
  network: {
    city: "cta",                // NETWORK_CITIES key (Chicago | Boston)
    data: null,                 // NetworkData (lazy, cached across tab switches)
    map: null,                  // NetworkMap (owned by NetworkView)
    filters: { routes: [], direction: null,
               periods: ["am_peak", "pm_peak"], daytype: "weekday", dow: null,
               pick: null, weather: null },
    metric: "overall",          // METRICS key (family or rate/static metric)
    stat: "mean",               // mean|median|std|p95|buffer (delay families)
    compare: null,              // null | "peak" | "selection"
    checkedRoutes: [],          // sticky route checkboxes
    activeRoute: null,          // transient highlighted route row
    minN: 10,
    selected: null,
  },
};

window.__S = S; // dev/testing handle
const trajView = new TrajectoryView(S);
const speedView = new SpeedView(S);
const networkView = new NetworkView(S);
const areasView = new AreasView(S);
const exploreView = new ExploreView(S);

// Display modes. Rich = every source + delay row; Lite = the primary source +
// its inferred delays only (emulating the single-trip route dashboard). Modes
// are just preset toggle states over the shared views — no separate chart code.
const MODE_PRESETS = {
  rich: { phoneCurve: true, phoneRaw: false, r2Curve: true, r2Raw: false, stops: false,
          phoneSpeed: true, r2Speed: true, dAVL: true, dWeb: true, dLoss: false },
  lite: { phoneCurve: true, phoneRaw: false, r2Curve: false, r2Raw: false, stops: false,
          phoneSpeed: true, r2Speed: false, dAVL: false, dWeb: false, dLoss: false },
};
const TOGGLE_IDS = {
  "t-phoneCurve": "phoneCurve", "t-phoneRaw": "phoneRaw", "t-r2Curve": "r2Curve", "t-r2Raw": "r2Raw", "t-stops": "stops",
  "s-phoneSpeed": "phoneSpeed", "s-r2Speed": "r2Speed", "s-dAVL": "dAVL", "s-dWeb": "dWeb",
  "s-dLoss": "dLoss",
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
  syncHash();
  if (S.main === "network") { renderNetwork(); return; }
  teardownNetwork();
  renderSingle();
}

// Network: the NetworkMap persists across map/areas sub-tabs (areas rows
// highlight on it); NetworkData persists across main-tab switches.
async function renderNetwork() {
  teardownMap();
  $("trip-meta").textContent = ""; // trip info is irrelevant on this tab
  const xp = document.getElementById("explore-panel");
  if (S.ntab === "explore") {
    // Fact-table querying stands alone: no map, no payload download.
    document.body.classList.remove("show-map", "network-mode", "network-areas");
    xp.classList.remove("hidden");
    exploreView.render(xp);
    return;
  }
  xp.classList.add("hidden");
  document.body.classList.add("show-map", "network-mode");
  document.body.classList.remove("network-areas"); // areas sub-tab hidden
  S.network.syncHash = syncHash;
  if (!S.network.data) {
    try {
      // Cache the init promise: startup can trigger two renders (hash apply +
      // init flow) and both must share one NetworkData (and one download set).
      if (!S.network._dataPromise) {
        S.network._dataPromise =
          new NetworkData(NETWORK_CITIES[S.network.city].base).init();
      }
      S.network.data = await S.network._dataPromise;
    } catch (err) {
      $("chart").innerHTML = `<div class="nw-empty">Network payloads missing —
        run analysis/network/build_payloads.py.<br><code>${err}</code></div>`;
      return;
    }
  }
  document.querySelectorAll("#ntabs [data-ncity]").forEach((b) =>
    b.classList.toggle("active", b.dataset.ncity === S.network.city));
  await networkView.render();
  $("chart").innerHTML = "";
}

// Full reset: per-city payloads, route indices, selections, and the map
// (camera + source) are all city-specific.
function switchNetworkCity(city) {
  if (!NETWORK_CITIES[city] || city === S.network.city) return;
  networkView.destroy();
  const N = S.network;
  N.city = city;
  N.data = null;
  N._dataPromise = null;
  N.selected = null;
  N.checkedRoutes = [];
  N.activeRoute = null;
  N.pendingSeg = null;
  N.filters.routes = [];
  N.filters.direction = null;
  render();
}

function teardownNetwork() {
  if (!document.body.classList.contains("network-mode")) return;
  document.body.classList.remove("network-mode", "network-areas");
  networkView.destroy();
}

function renderSingle() {
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

// ---------------------------------------------------------------- deep links
// Shareable hash routes: #single/trajectory, #single/speed, #network/map,
// #network/explore. Network adds ?metric=<key>…; single adds the trip and
// its controls: ?trip=<key>&mode=lite&full=1&x=distance&on=a.b&off=c.d
// (on/off = checkbox toggles that differ from the mode's preset).
// The hash is updated on navigation and applied on load / back-forward.

function currentHash() {
  let h = `#${S.main}`;
  const sub = { single: S.tab, network: S.ntab }[S.main];
  if (sub) h += `/${sub}`;
  if (S.main === "single") {
    const q = new URLSearchParams();
    const key = $("trip-select")?.value;
    if (key) q.set("trip", key);
    const mode = S.mode ?? "rich";
    if (mode !== "rich") q.set("mode", mode);
    if (S.showFull) q.set("full", "1");
    if (S.speedX !== "time") q.set("x", S.speedX);
    const preset = MODE_PRESETS[mode];
    const on = [], off = [];
    for (const k of Object.keys(preset)) {
      if (S.toggles[k] && !preset[k]) on.push(k);
      else if (!S.toggles[k] && preset[k]) off.push(k);
    }
    if (on.length) q.set("on", on.join("."));
    if (off.length) q.set("off", off.join("."));
    const qs = q.toString();
    if (qs) h += `?${qs}`;
  }
  if (S.main === "network") {
    const N = S.network;
    const F = N.filters;
    const q = new URLSearchParams();
    if (N.city !== "cta") q.set("city", N.city);
    q.set("metric", N.metric);
    q.set("stat", N.stat);
    if (N.compare) q.set("cmp", N.compare);
    const defPeriods = ["am_peak", "pm_peak"];
    if (F.periods.join(".") !== defPeriods.join(".")) q.set("periods", F.periods.join("."));
    if (F.dow != null) q.set("days", `dow${F.dow}`);
    else if (F.daytype !== "weekday") q.set("days", F.daytype ?? "everyday");
    if (F.weather != null) q.set("weather", F.weather);
    const rids = N.data?.meta?.dims?.route_ids;
    if (rids) {
      if ((N.checkedRoutes ?? []).length) {
        q.set("routes", N.checkedRoutes.map((i) => rids[i]).join("."));
      }
      if (N.activeRoute != null) q.set("active", rids[N.activeRoute]);
    }
    if (F.direction) q.set("dir", F.direction);
    if (N.minN !== 10) q.set("minn", N.minN);
    if (N.selected != null) {
      // Stable deep links: seg= carries the seg_id (SIG_<up>__SIG_<down>),
      // which survives registry rebuilds; the numeric sid does not.
      const f = N.data?.segments?.features?.find(
        (x) => x.properties.sid === N.selected);
      q.set("seg", f ? f.properties.seg_id : N.selected);
    }
    h += `?${q.toString()}`;
  }
  return h;
}

function syncHash() {
  if (S._applyingHash) return;
  const h = currentHash();
  if (location.hash !== h) history.replaceState(null, "", h);
}

function applyHash() {
  const raw = (location.hash || "").replace(/^#/, "");
  if (!raw) return false;
  const [path, query] = raw.split("?");
  const [main, sub] = path.split("/");
  if (!["single", "network"].includes(main)) return false;
  S._applyingHash = true;
  if (main === "single" && ["trajectory", "speed"].includes(sub)) S.tab = sub;
  if (main === "network" && S.ntab !== "explore") S.ntab = "map";
  const params = new URLSearchParams(query || "");
  if (main === "single") applySingleParams(params);
  if (main === "network") {
    const N = S.network;
    const F = N.filters;
    const city = params.get("city") ?? "cta";
    if (NETWORK_CITIES[city] && city !== N.city) {
      // City change via URL: everything downstream is city-specific.
      networkView.destroy();
      N.city = city;
      N.data = null;
      N._dataPromise = null;
      N.selected = null;
      N.checkedRoutes = [];
      N.activeRoute = null;
      F.routes = [];
      F.direction = null;
    }
    const metric = params.get("metric");
    if (metric && METRICS[metric]) N.metric = metric;
    const stat = params.get("stat");
    if (["mean", "median", "std", "p95", "buffer"].includes(stat ?? "")) N.stat = stat;
    const cmp = params.get("cmp");
    N.compare = ["peak", "selection"].includes(cmp ?? "") ? cmp : null;
    const periods = params.get("periods");
    if (periods) {
      const valid = ["am_peak", "midday", "pm_peak", "evening", "late_night"];
      const ps = periods.split(".").filter((x) => valid.includes(x));
      if (ps.length) F.periods = ps;
    }
    const days = params.get("days");
    if (days != null) {
      if (/^dow[0-6]$/.test(days)) { F.dow = Number(days.slice(3)); F.daytype = null; }
      else if (days === "everyday") { F.daytype = null; F.dow = null; }
      else if (["weekday", "weekend"].includes(days)) { F.daytype = days; F.dow = null; }
    }
    F.weather = params.has("weather") ? Number(params.get("weather")) : F.weather;
    // Route ids can't map to indices until NetworkData's meta is loaded —
    // stash names; NetworkView resolves them on first render.
    if (params.has("routes")) N.pendingRoutes = params.get("routes").split(".");
    if (params.has("active")) N.pendingActive = params.get("active");
    if (params.has("dir")) F.direction = params.get("dir");
    if (params.has("minn")) N.minN = Math.max(1, Number(params.get("minn")) || 10);
    if (params.has("seg")) {
      const v = params.get("seg");
      if (/^SIG_/.test(v)) N.pendingSegId = v;
      else N.pendingSeg = Number(v);   // legacy numeric link -> warned in view
    }
  }
  // reflect sub-tab button states
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === S.tab));
  document.querySelectorAll("#ntabs button").forEach((b) => b.classList.toggle("active", b.dataset.ntab === S.ntab));
  const metricSel = $("nw-metric");
  if (metricSel && metricSel.options.length) metricSel.value = S.network.metric;
  setMain(main);
  S._applyingHash = false;
  return true;
}

// Single-trip URL params -> state + controls. The trip itself loads async:
// on startup init() picks S.pendingTrip; later (edited URL / back-forward)
// a different key is loaded here.
function applySingleParams(params) {
  const mode = params.get("mode") === "lite" ? "lite" : "rich";
  S.mode = mode;
  Object.assign(S.toggles, MODE_PRESETS[mode]);
  const flip = (name, v) => {
    for (const k of (params.get(name) ?? "").split(".")) {
      if (k in MODE_PRESETS.rich) S.toggles[k] = v;
    }
  };
  flip("on", true);
  flip("off", false);
  S.showFull = params.get("full") === "1";
  S.speedX = params.get("x") === "distance" ? "distance" : "time";
  S.view = { trajectory: null, speed: null };
  S.cursor = null;
  syncSingleControls();
  const key = params.get("trip");
  if (!key) return;
  if (!S.trip) { S.pendingTrip = key; return; } // startup: init() loads it
  const sel = $("trip-select");
  if (sel && sel.value !== key && [...sel.options].some((o) => o.value === key)) {
    sel.value = key;
    loadTrip(key);
  }
}

function syncSingleControls() {
  for (const [id, key] of Object.entries(TOGGLE_IDS)) { const el = $(id); if (el) el.checked = S.toggles[key]; }
  document.querySelectorAll("#modes button").forEach((b) =>
    b.classList.toggle("active", b.dataset.mode === (S.mode ?? "rich")));
  const full = $("show-full"); if (full) full.checked = S.showFull;
  document.querySelectorAll('input[name="speedx"]').forEach((r) => { r.checked = r.value === S.speedX; });
  const loss = $("s-dLoss");
  if (loss) { loss.disabled = !S.toggles.dAVL; loss.parentElement.style.opacity = S.toggles.dAVL ? "" : "0.45"; }
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

// Optional site config (written by scripts/build_public_site.py; absent in
// the local full build): which Network cities and sub-tabs this deployment
// exposes, e.g. {"network_cities": ["mbta", "translink"], "explore": false}.
async function applySiteConfig() {
  const cfg = await fetch("site.json", { cache: "no-cache" })
    .then((r) => (r.ok ? r.json() : null)).catch(() => null);
  if (!cfg) return;
  if (Array.isArray(cfg.network_cities) && cfg.network_cities.length) {
    const keep = new Set(cfg.network_cities.filter((c) => NETWORK_CITIES[c]));
    for (const c of Object.keys(NETWORK_CITIES)) if (!keep.has(c)) delete NETWORK_CITIES[c];
    document.querySelectorAll("#ntabs [data-ncity]").forEach((b) => {
      if (!keep.has(b.dataset.ncity)) b.remove();
    });
    if (!keep.has(S.network.city)) S.network.city = cfg.default_city ?? [...keep][0];
  }
  if (cfg.explore === false) {
    document.querySelector('#ntabs [data-ntab="explore"]')?.remove();
    if (S.ntab === "explore") S.ntab = "map";
  }
}

async function init() {
  await applySiteConfig();
  // Tolerate a payload set with no single-trip data (e.g. network-only build):
  // the Network tab must still work.
  let items = [];
  try {
    const idx = await fetch("../data/index.json", { cache: "no-store" }).then((r) => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.json();
    });
    items = idx.items;
  } catch (e) {
    console.warn("no data/index.json — single-trip tab will be empty", e);
  }
  const trips = items.filter((i) => i.kind === "trip");

  const tripSel = $("trip-select");
  for (const it of trips) {
    const o = document.createElement("option");
    o.value = it.key; o.textContent = it.label;
    tripSel.appendChild(o);
  }
  tripSel.onchange = () => loadTrip(tripSel.value);

  // Main tabs (single / network).
  document.querySelectorAll("#main-tabs button").forEach((b) =>
    (b.onclick = () => setMain(b.dataset.main)));

  // Single-trip sub-tabs
  document.querySelectorAll("#tabs button").forEach((b) =>
    (b.onclick = () => {
      document.querySelectorAll("#tabs button").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      S.tab = b.dataset.tab; render();
    }));
  // Network sub-tabs (city buttons are separate — they carry data-ncity)
  document.querySelectorAll("#ntabs button[data-ntab]").forEach((b) =>
    (b.onclick = () => {
      document.querySelectorAll("#ntabs button[data-ntab]").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      S.ntab = b.dataset.ntab; render();
    }));
  document.querySelectorAll("#ntabs button[data-ncity]").forEach((b) =>
    (b.onclick = () => switchNetworkCity(b.dataset.ncity)));

  const bind = (id, key) => { $(id).onchange = (e) => { S.toggles[key] = e.target.checked; render(); }; };
  bind("t-phoneCurve", "phoneCurve"); bind("t-phoneRaw", "phoneRaw");
  bind("t-r2Curve", "r2Curve"); bind("t-r2Raw", "r2Raw"); bind("t-stops", "stops");
  bind("s-phoneSpeed", "phoneSpeed"); bind("s-r2Speed", "r2Speed");
  bind("s-dAVL", "dAVL"); bind("s-dWeb", "dWeb"); bind("s-dLoss", "dLoss");
  // stop loss is meaningless without the door cycles that define it
  const syncLoss = () => {
    const el = $("s-dLoss"); if (!el) return;
    el.disabled = !S.toggles.dAVL;
    el.parentElement.style.opacity = S.toggles.dAVL ? "" : "0.45";
  };
  $("s-dAVL")?.addEventListener("change", syncLoss);
  syncLoss();
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
  // single-trip: M/S basemap, H hide-features (speed tab map); network: M/S.
  document.addEventListener("keydown", (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    const k = e.key.toLowerCase();
    const hide = () => { S._hideUnattr = !S._hideUnattr; return S._hideUnattr; };
    if (S.main === "network" && S.network.map) {
      if (k === "m") S.network.map.setBasemap("map");
      else if (k === "s") S.network.map.setBasemap("satellite");
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

  window.addEventListener("hashchange", () => { applyHash(); });

  const routed = applyHash();
  if (routed && S.main === "network") {
    // network payloads need no trip; still load trips in the background for tab switches
    if (trips.length) { tripSel.value = trips[0].key; loadTrip(trips[0].key); }
  } else if (trips.length) {
    const want = trips.some((t) => t.key === S.pendingTrip) ? S.pendingTrip : trips[0].key;
    S.pendingTrip = null;
    tripSel.value = want; await loadTrip(want);
  }
  else { setMain("network"); }  // network-only payload set: land on the map
}

init().catch((err) => {
  console.error(err);
  $("chart").innerHTML = `<pre style="color:#c00;padding:16px">${err.stack || err}</pre>`;
});
