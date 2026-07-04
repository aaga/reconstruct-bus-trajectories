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
import { $, busElement, interp } from "./chart_util.js";
import { TrajectoryView } from "./views/trajectory_view.js";
import { SpeedView } from "./views/speed_view.js";

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

const trajView = new TrajectoryView(S);
const speedView = new SpeedView(S);

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
  if (isSpeed) speedView.render(); else trajView.render();
  if (isSpeed) { ensureMap(); setTimeout(() => S.mapView?.resize(), 60); }
}

async function loadTrip(key) {
  teardownMap();
  S.trip = await fetch(`../data/${key}.json`, { cache: "no-store" }).then((r) => r.json());
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
  const idx = await fetch("../data/index.json", { cache: "no-store" }).then((r) => r.json());
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
