// Controller: city/provider selection, trip setup (vehicle-number or
// nearby-stop), the event state machine, sensor wiring, rehydrate-on-load,
// map + upcoming-stops panels, GPS-driven next stop, summary/export.
//
// Truth lives in IndexedDB (storage.js). The in-memory `S` below is a cache
// of it; every mutation writes through immediately, so backgrounding the
// tab, locking the phone, or even a full reload mid-trip loses nothing but
// the sensor samples the OS suppressed while we were hidden.
//
// Everything agency-specific lives behind a provider module (providers/*.js);
// this file only ever sees the normalized shapes documented in providers/index.js.

import maplibregl from "https://cdn.jsdelivr.net/npm/maplibre-gl@4.7.1/+esm";
import * as db from "./storage.js";
import * as sensors from "./sensors.js";
import * as geo from "./geo.js";
import { CITIES, DEFAULT_CITY, getCityConfig, loadProvider, isGeneric } from "./providers/index.js";
import * as exp from "./export.js";
import * as sync from "./sync.js";

// ------------------------------------------------------------ event types
//
// `button` (0–5) marks the six dedicated stop-reason buttons and fixes their
// position — that order NEVER changes. Types without `button` (passenger,
// exit_wait, note) are reachable only through the editor dropdown or a
// dedicated control. `sub` flags an in-banner follow-up prompt: turn_delay
// asks left/right.
export const EVENT_TYPES = {
  bus_stop:    { label: "🚏 Bus stop\n(door open)", short: "🚏 Bus stop",   color: "#3a85d6", button: 0 },
  red_light:   { label: "🔴 Red light",             short: "🔴 Red light",  color: "#cc0000", button: 1 },
  congestion:  { label: "🚗 Congestion",            short: "🚗 Congestion", color: "#e0a800", button: 2 },
  turn_delay:  { label: "🔄 Turn delay",            short: "🔄 Turn delay", color: "#00897b", button: 3, sub: "direction" },
  driver_hold: { label: "⏸️ Driver hold",           short: "⏸️ Driver hold",color: "#7b3fa0", button: 4 },
  other:       { label: "✏️ Other",                 short: "✏️ Other",      color: "#616161", button: 5 },
  passenger:   { label: "🧍 Passenger disruption",  short: "🧍 Passenger",  color: "#d81b60" },
  exit_wait:   { label: "🔀 Waiting to merge",      short: "🔀 Merge wait", color: "#5c8ab8" },
  note:        { label: "📝 Note",                  short: "📝 Note",       color: "#546e7a" },
};

// Fixed left-to-right, top-to-bottom button order. Do not sort/rebuild.
const BUTTON_ORDER = ["bus_stop", "red_light", "congestion", "turn_delay", "driver_hold", "other"];

// Stop-anchored event types (carry a stop id/name; editor shows the stop picker).
const STOP_TYPES = new Set(["bus_stop", "exit_wait"]);

const BUFFER_M = 27.43;             // 90 ft — keep a stop "next" until we're this far past it
const GPS_GOOD_ACCURACY_M = 30;     // trust GPS for the next stop only at/under this accuracy
const NEARBY_STOP_MAX_M = 150;      // nearby-stop start: only offer stops this close
const MAX_BUS_ETA_MIN = 15;         // nearby-stop start: only offer buses arriving within this

const $ = (id) => document.getElementById(id);

// --------------------------------------------------------------- app state

let provider = null;        // the loaded transit provider module for the active/selected city
let allStopsCache = null;   // every stop for the current city (editor stop picker), lazy
let pendingVehicle = null;  // normalized Vehicle awaiting confirmation, before a trip starts

const S = {
  meta: null,            // active tripMeta record
  activeEvent: null,     // open event record, or null = MOVING
  lastPing: null,
  pingCount: 0,
  motionBuf: [],         // samples awaiting a batched IndexedDB write
  motionWindow: [],      // recent sample times, for the Hz readout
  gps: null,
  motion: null,
  wakeLock: null,
  patternStopsArr: null, // ordered PatternStop[], or null if unbundled
  patternStops: null,    // Map stop_id -> PatternStop, for quick lookups
  patternShape: null,    // [[lon,lat,cumDist_m], ...], or null
  predictions: [],       // latest StopPrediction[] from the provider (ETAs + fallback)
  etaByStop: new Map(),  // stop_id -> eta_min
  nextStop: null,        // {stop_id, name, near_side}
  nextStopIdx: -1,       // index of nextStop within patternStopsArr (-1 if unknown)
  gridButtons: {},       // type -> button element (built once)
  generic: false,        // Generic agency: no feed, phone-only map, no stop data
  map: null,
  phoneMarker: null,
  busMarker: null,
  timers: [],
};

function showView(name) {
  for (const v of ["setup", "recording", "summary"]) {
    $(`view-${v}`).classList.toggle("hidden", v !== name);
  }
}

function every(ms, fn) { S.timers.push(setInterval(fn, ms)); }
function clearTimers() { S.timers.forEach(clearInterval); S.timers = []; }

// ============================================================== SETUP VIEW

function setupStep(name) {
  for (const s of ["main", "stops", "buses", "confirm"]) {
    $(`setup-${s}`).classList.toggle("hidden", s !== name);
  }
}

function populateCities() {
  const sel = $("city-select");
  sel.innerHTML = "";
  for (const c of Object.values(CITIES)) {
    const o = document.createElement("option");
    o.value = c.id;
    o.textContent = c.label;
    sel.appendChild(o);
  }
  const saved = localStorage.getItem("city");
  sel.value = saved && CITIES[saved] ? saved : DEFAULT_CITY;
  applyCityConfig(sel.value);
}

function applyCityConfig(id) {
  $("vehicle-id").placeholder = getCityConfig(id).vehiclePlaceholder;
  // Generic agency: no vehicle lookup and no nearby-stop start — the button
  // just starts recording. Hide the agency-specific affordances.
  const generic = isGeneric(id);
  $("btn-lookup").textContent = generic ? "▶ Start" : "🔎 Look up bus";
  $("btn-find-stop").classList.toggle("hidden", generic);
  $("lookup-hint").classList.toggle("hidden", generic);
}

function onCityChange() {
  const id = $("city-select").value;
  localStorage.setItem("city", id);
  applyCityConfig(id);
  provider = null;          // force a reload for the new city on next use
  allStopsCache = null;
  if (!isGeneric(id)) ensureProvider();  // eager preload so the first lookup is fast
}

// Make a missing sync token impossible to miss when auto-save is on.
function updateSyncWarn() {
  const on = $("sync-enabled").checked;
  const missing = !$("sync-token").value.trim();
  $("sync-warn").classList.toggle("hidden", !(on && missing));
}

/** Load (once) the provider for the currently selected city. Null for Generic. */
async function ensureProvider() {
  const id = $("city-select").value;
  if (isGeneric(id)) return null;
  if (!provider) provider = await loadProvider(id);
  return provider;
}

// ----- Method A: look up by vehicle number (or, for Generic, start directly)

async function onLookup() {
  if (isGeneric($("city-select").value)) return startGeneric();
  const p = await ensureProvider();
  const vid = p.validateVehicleId($("vehicle-id").value);
  if (!vid) {
    $("setup-status").textContent = "Enter a valid vehicle number.";
    return;
  }
  $("setup-status").textContent = `Looking up bus #${vid}…`;
  try {
    const vehicle = await p.getVehicle(vid);
    $("setup-status").textContent = "";
    await showConfirm(vehicle);
  } catch (err) {
    $("setup-status").textContent =
      `Couldn't find bus #${vid}: ${err.message || err}. ` +
      `It must be a bus that's currently in service.`;
  }
}

// Generic agency: no lookup and no confirm — validate the vehicle number and go
// straight into recording. There's nothing to confirm without an agency feed.
async function startGeneric() {
  const vid = $("vehicle-id").value.trim();
  if (!vid) {
    $("setup-status").textContent = "Enter the bus vehicle number.";
    return;
  }
  pendingVehicle = { bus_id: vid };
  await onStartTrip();
}

// ----- Method B: start from a nearby stop

async function onFindStop() {
  setupStep("stops");
  $("stops-list").innerHTML = "";
  $("stops-status").textContent = "Getting your location…";
  const p = await ensureProvider();
  let pos;
  try {
    pos = await sensors.getCurrentPosition();
  } catch {
    $("stops-status").textContent = "Location unavailable — allow location access and retry.";
    return;
  }
  try {
    const stops = (await p.getNearbyStops(pos.lat, pos.lon, 25))
      .filter((s) => s.dist_m <= NEARBY_STOP_MAX_M);
    renderPickList($("stops-list"), stops.map((s) => ({
      main: s.name,
      sub: `${Math.round(s.dist_m)} m away`,
      onPick: () => onPickStop(s),
    })));
    $("stops-status").textContent = stops.length
      ? "Tap the stop you're at:"
      : `No stops within ${NEARBY_STOP_MAX_M} m — move closer to a stop and retry.`;
  } catch (err) {
    $("stops-status").textContent = `Couldn't load stops: ${err.message || err}`;
  }
}

async function onPickStop(stop) {
  setupStep("buses");
  $("buses-title").textContent = stop.name;
  $("buses-list").innerHTML = "";
  $("buses-status").textContent = "Loading upcoming buses…";
  const p = await ensureProvider();
  try {
    const preds = (await p.getPredictions(stop.stop_id))
      .filter((pr) => pr.eta_min != null && pr.eta_min <= MAX_BUS_ETA_MIN);
    renderPickList($("buses-list"), preds.map((pr) => ({
      main: `${pr.route_id || "?"} → ${pr.destination || ""}`.trim(),
      sub: `#${pr.vehicle_id || "?"} · ${etaText(pr.eta_min)}`,
      onPick: () => onPickBus(pr),
    })));
    $("buses-status").textContent = preds.length
      ? "Tap the bus you're boarding:"
      : `No buses arriving within ${MAX_BUS_ETA_MIN} min.`;
  } catch (err) {
    $("buses-status").textContent = `Couldn't load buses: ${err.message || err}`;
  }
}

async function onPickBus(pred) {
  if (!pred.vehicle_id) {
    $("buses-status").textContent = "That arrival has no live vehicle id — pick another.";
    return;
  }
  $("buses-status").textContent = `Looking up bus #${pred.vehicle_id}…`;
  const p = await ensureProvider();
  try {
    const vehicle = await p.getVehicle(pred.vehicle_id);
    await showConfirm(vehicle);
  } catch (err) {
    $("buses-status").textContent = `Couldn't look up that bus: ${err.message || err}`;
  }
}

// ----- Confirmation (both methods converge here)

async function showConfirm(vehicle) {
  pendingVehicle = vehicle;
  const p = await ensureProvider();
  let nextName = "—";
  try {
    const preds = await p.getNextStops(vehicle.bus_id);
    if (preds[0]) nextName = preds[0].stop_name;
  } catch { /* the confirmation can show without a next stop */ }

  const row = (k, v) =>
    `<div class="row"><span>${k}</span><strong>${escapeHtml(String(v))}</strong></div>`;
  $("confirm-card").innerHTML =
    row("Route", vehicle.route_id || "—") +
    row("Destination", vehicle.destination || "—") +
    row("Next stop", nextName) +
    row("Vehicle", "#" + vehicle.bus_id);
  setupStep("confirm");
}

async function onStartTrip() {
  if (!pendingVehicle) return;
  // iOS only grants devicemotion from inside the tap gesture — do it now.
  await sensors.requestMotionPermission();

  const city = $("city-select").value;
  const v = pendingVehicle;
  const now = Date.now();
  const meta = {
    key: `${now}_${v.trip_id || v.bus_id}`,
    city,
    trip_id: String(v.trip_id || ""),
    bus_id: String(v.bus_id),
    route_id: String(v.route_id || ""),
    pattern_id: String(v.pattern_id || ""),
    destination: v.destination || "",
    observer: $("observer-name").value.trim(),
    start_t: now,
    end_t: null,
    status: "active",
    gaps: [],
  };
  localStorage.setItem("observer_name", meta.observer);
  pendingVehicle = null;
  await db.putTripMeta(meta);
  await beginRecording(meta, null);
}

// ========================================================== RECORDING VIEW

async function beginRecording(meta, openEvent) {
  S.meta = meta;
  S.activeEvent = openEvent;
  S.generic = isGeneric(meta.city);
  S.pingCount = await db.countPings(meta.key);
  resetPatternState();
  if (!S.generic) {
    if (!provider) provider = await loadProvider(meta.city || DEFAULT_CITY);
    loadPattern(meta.pattern_id);
  }

  // Generic has no stop/route data: hide the next-stop banner + the upcoming-
  // stops toggle. The map (phone-only) stays.
  $("next-stop").classList.toggle("hidden", S.generic);
  $("btn-stops-toggle").classList.toggle("hidden", S.generic);

  showView("recording");
  buildEventGrid();        // one-time; never rebuilt
  refreshControls();
  if (!S.generic) renderNextStop();
  await renderEventList();
  startSensors();
  initMap();

  every(1000, uiTick);
  if (!S.generic) {
    every(20000, pollPredictions);
    every(15000, pollVehicle);
    pollPredictions();
    pollVehicle();
  }

  if ($("sync-enabled").checked || localStorage.getItem("sync_enabled") === "1") {
    sync.start(meta.key, (status) => { $("st-sync").textContent = status; });
  }
}

function resetPatternState() {
  S.patternStopsArr = null;
  S.patternStops = null;
  S.patternShape = null;
  S.predictions = [];
  S.etaByStop = new Map();
  S.nextStop = null;
  S.nextStopIdx = -1;
}

// Load the ordered stops + shape for a pattern. Guarded against a pattern
// change landing while a previous fetch is still in flight.
function loadPattern(patternId) {
  const pid = String(patternId);
  provider.getPatternStops(pid).then((stops) => {
    if (S.meta?.pattern_id !== pid) return;
    S.patternStopsArr = stops;
    S.patternStops = stops ? new Map(stops.map((s) => [s.stop_id, s])) : null;
  });
  provider.getPatternShape(pid).then((shape) => {
    if (S.meta?.pattern_id !== pid) return;
    S.patternShape = shape;
  });
}

// ------------------------------------------------------------ sensors

function startSensors() {
  stopSensors();
  S.gps = sensors.startGps(onPing, () => { S.lastPing = S.lastPing; });
  S.motion = sensors.startMotion(onMotionSample);
  sensors.acquireWakeLock().then((wl) => { S.wakeLock = wl; });
}

function stopSensors() {
  S.gps?.stop(); S.gps = null;
  S.motion?.stop(); S.motion = null;
  S.wakeLock?.release(); S.wakeLock = null;
  flushMotion();
}

function onPing(p) {
  S.lastPing = p;
  S.pingCount += 1;
  db.addPing({ key: S.meta.key, ...p });
}

function onMotionSample(s) {
  S.motionBuf.push(s);
  S.motionWindow.push(s.t);
  if (S.motionBuf.length >= 120) flushMotion();
}

function flushMotion() {
  if (!S.motionBuf.length || !S.meta) return;
  const batch = S.motionBuf;
  S.motionBuf = [];
  db.addMotionBatch(S.meta.key, batch);
}

// ------------------------------------------------------- state machine

function nowPos() {
  return { lat: S.lastPing?.lat ?? null, lon: S.lastPing?.lon ?? null };
}

/** Close the active event (if any) and open `type`; null type = MOVING. */
async function setActive(type) {
  const t = Date.now();
  const pos = nowPos();
  const prev = S.activeEvent;

  if (S.activeEvent) {
    if (type === S.activeEvent.type) type = null; // tapping active type -> moving
    await db.updateEvent(S.activeEvent.id, { end_t: t, end_lat: pos.lat, end_lon: pos.lon });
    S.activeEvent = null;
  }

  if (type) {
    const ev = newEvent(type, t, pos);
    if (type === "bus_stop") {
      ev.stop_id = S.nextStop?.stop_id ?? null;
      ev.stop_name = S.nextStop?.name ?? null;
      ev.near_side = S.nextStop?.near_side ?? null;
    }
    // Waiting-to-merge is a continuation of the stop it followed: link it and
    // carry the stop forward for context.
    if (type === "exit_wait" && prev?.type === "bus_stop") {
      ev.parent_id = prev.id;
      ev.stop_id = prev.stop_id ?? null;
      ev.stop_name = prev.stop_name ?? null;
    }
    S.activeEvent = ev;
    await db.putEvent(ev);
  }

  refreshControls();
  await renderEventList();
  if (type === "other") openEditor(S.activeEvent.id);
}

function newEvent(type, t, pos) {
  return {
    id: crypto.randomUUID(),
    key: S.meta.key,
    trip_id: S.meta.trip_id,
    type,
    note: "",
    start_t: t,
    end_t: null,
    start_lat: pos.lat, start_lon: pos.lon,
    end_lat: null, end_lon: null,
    parent_id: null,
    direction: null,
    stop_id: null,
    stop_name: null,
    near_side: null,
    pax_on: 0,
    pax_off: 0,
  };
}

// Passenger counter: each tap bumps the active bus-stop event's on/off count.
async function incrementPax(field) {
  const e = S.activeEvent;
  if (!e || e.type !== "bus_stop") return;
  S.activeEvent = await db.updateEvent(e.id, { [field]: (e[field] || 0) + 1 });
  renderPaxControls();
}

// A note is an instantaneous, timestamped marker — it does NOT change the
// MOVING/active state, so it can be added at any time.
async function addNote() {
  if (!S.meta) return;
  const t = Date.now();
  const pos = nowPos();
  const ev = newEvent("note", t, pos);
  ev.end_t = t;
  ev.end_lat = pos.lat;
  ev.end_lon = pos.lon;
  await db.putEvent(ev);
  await renderEventList();
  openEditor(ev.id);
}

async function setTurnDirection(dir) {
  if (!S.activeEvent || S.activeEvent.type !== "turn_delay") return;
  S.activeEvent = await db.updateEvent(S.activeEvent.id, { direction: dir });
  renderTurnControls();
}

// --------------------------------------------------------------- rendering

function buildEventGrid() {
  const grid = $("event-grid");
  grid.innerHTML = "";
  S.gridButtons = {};
  for (const type of BUTTON_ORDER) {
    const cfg = EVENT_TYPES[type];
    const btn = document.createElement("button");
    btn.className = "event-btn";
    btn.style.background = cfg.color;
    btn.innerHTML = cfg.label.replace("\n", "<br>");
    btn.onclick = () => setActive(type);
    grid.appendChild(btn);
    S.gridButtons[type] = btn;
  }
}

/** Update highlights + action-row + banner. Never rebuilds the grid. */
function refreshControls() {
  const active = S.activeEvent;
  const t = active?.type ?? null;
  for (const [type, btn] of Object.entries(S.gridButtons)) {
    btn.classList.toggle("active-type", t === type);
  }
  // Action row: space is reserved even while moving (visibility, not display)
  // so the grid below never shifts. Moving alone (full width) for most events,
  // or Moving + Waiting-to-merge (half each) while at / leaving a bus stop.
  $("action-row").classList.toggle("reserved", !active);
  const showExit = t === "bus_stop" || t === "exit_wait";
  $("btn-exit").classList.toggle("hidden", !showExit);
  $("btn-exit").classList.toggle("active-type", t === "exit_wait");
  renderBanner();
}

function renderBanner() {
  const banner = $("active-banner");
  const e = S.activeEvent;
  $("btn-edit-active").classList.toggle("hidden", !e);
  if (!e) {
    banner.className = "active-banner moving";
    banner.style.background = "";
    $("active-label").textContent = "MOVING";
    $("active-sub").textContent = "";
    $("active-duration").textContent = "";
  } else {
    const cfg = EVENT_TYPES[e.type];
    banner.className = "active-banner";
    banner.style.background = cfg.color;
    $("active-label").textContent = cfg.short;
    $("active-sub").textContent =
      e.type === "bus_stop" && e.stop_name ? e.stop_name : (e.note || "");
    // Show the duration from the get-go (0:00) so the banner height is stable
    // from the moment the event opens — no jump when the 1 s tick first fills it.
    $("active-duration").textContent = fmtDur(Date.now() - e.start_t);
  }
  renderTurnControls();
  renderPaxControls();
}

function renderTurnControls() {
  const e = S.activeEvent;
  const show = e && e.type === "turn_delay";
  $("turn-controls").classList.toggle("hidden", !show);
  if (!show) return;
  $("btn-turn-left").classList.toggle("selected", e.direction === "left");
  $("btn-turn-right").classList.toggle("selected", e.direction === "right");
}

// In-banner passenger counter, shown while a bus-stop event is active.
function renderPaxControls() {
  const e = S.activeEvent;
  const show = e && e.type === "bus_stop";
  $("pax-controls").classList.toggle("hidden", !show);
  if (!show) return;
  // Sign rides on the number once it leaves 0 (+3 / −2), not on the caption.
  $("pax-on-count").textContent = e.pax_on ? `+${e.pax_on}` : "0";
  $("pax-off-count").textContent = e.pax_off ? `−${e.pax_off}` : "0";
}

function fmtDur(ms) {
  const s = Math.floor(ms / 1000);
  const m = Math.floor(s / 60);
  return m >= 60
    ? `${Math.floor(m / 60)}:${String(m % 60).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`
    : `${m}:${String(s % 60).padStart(2, "0")}`;
}

function etaText(min) {
  return min == null ? "" : (min <= 0 ? "due" : `${min} min`);
}

function uiTick() {
  if (!S.meta) return;
  $("st-elapsed").textContent = fmtDur(Date.now() - S.meta.start_t);

  // GPS staleness + accuracy. Coarse accuracy (network location) is the most
  // common iOS failure — surface it loudly so it gets fixed in Settings.
  const gpsEl = $("st-gps");
  if (S.lastPing) {
    const age = Math.round((Date.now() - S.lastPing.t) / 1000);
    const acc = S.lastPing.accuracy;
    gpsEl.textContent = `GPS ${age}s ago` + (acc != null ? ` ±${Math.round(acc)}m` : "");
    const coarse = acc != null && acc > 100;
    gpsEl.className = "stat " + (coarse ? "bad" : age > 8 ? "stale" : "ok");
    if (coarse) {
      $("gps-warn").classList.remove("hidden");
      $("gps-warn").textContent =
        `⚠ Coarse location (±${Math.round(acc)} m) — this is network, not GPS. ` +
        `Turn on Precise Location: Settings ▸ Privacy & Security ▸ Location ` +
        `Services ▸ Safari Websites ▸ Precise Location.`;
    } else {
      $("gps-warn").classList.add("hidden");
    }
  } else {
    gpsEl.textContent = "GPS waiting…";
    gpsEl.className = "stat stale";
  }

  const cutoff = Date.now() - 2000;
  S.motionWindow = S.motionWindow.filter((t) => t >= cutoff);
  $("st-accel").textContent = `Accel ${Math.round(S.motionWindow.length / 2)} Hz`;

  if (S.activeEvent) $("active-duration").textContent = fmtDur(Date.now() - S.activeEvent.start_t);
  if (S.motionBuf.length) flushMotion();

  if (S.generic) { updateGenericMap(); return; } // no stop data; just follow the phone
  updateNextStop(); // GPS-driven; cheap, no network
}

// Generic map: no bus feed — center on and follow the phone's own fix. Zooms
// in once on the first fix, then just recenters.
function updateGenericMap() {
  if (!S.map || $("map-panel").classList.contains("collapsed") || !S.lastPing) return;
  const ll = [S.lastPing.lon, S.lastPing.lat];
  S.phoneMarker.setLngLat(ll).addTo(S.map);
  if (!S._genericCentered) { S.map.jumpTo({ center: ll, zoom: 15 }); S._genericCentered = true; }
  else S.map.easeTo({ center: ll, duration: 500 });
}

async function renderEventList() {
  const events = (await db.getEvents(S.meta.key)).reverse();
  $("events-count").textContent = String(events.length);
  const el = $("event-list");
  el.innerHTML = "";
  for (const e of events) {
    const cfg = EVENT_TYPES[e.type] || { short: e.type, color: "#999" };
    const row = document.createElement("button");
    row.className = "event-row";
    const dur = e.type === "note" ? "" : (e.end_t ? fmtDur(e.end_t - e.start_t) : "active");
    const hhmm = new Date(e.start_t).toTimeString().slice(0, 8);
    row.innerHTML =
      `<span class="dot" style="background:${cfg.color}"></span>` +
      `<span class="what"><span class="type">${cfg.short}${detailSuffix(e)}</span>` +
      `<div class="note">${e.note ? escapeHtml(e.note) : ""}</div></span>` +
      `<span class="when">${hhmm}<br>${dur}</span>`;
    row.onclick = () => openEditor(e.id);
    el.appendChild(row);
  }
}

// Inline detail shown after the type name in the history list.
function detailSuffix(e) {
  if ((e.type === "bus_stop" || e.type === "exit_wait") && e.stop_name)
    return ` — ${escapeHtml(e.stop_name)}`;
  if (e.type === "turn_delay" && e.direction) return ` — ${e.direction}`;
  return "";
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

// Generic tappable list used by the setup pick screens (nearby stops, buses).
function renderPickList(el, items) {
  el.innerHTML = "";
  for (const it of items) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "pick-row";
    b.innerHTML =
      `<span class="pick-main">${escapeHtml(it.main)}</span>` +
      `<span class="pick-sub">${escapeHtml(it.sub)}</span>`;
    b.onclick = it.onPick;
    el.appendChild(b);
  }
}

// ------------------------------------------------------- next stop + upcoming

// Resolve the next stop from the phone's own fix when GPS is trustworthy,
// otherwise fall back to the provider's bus-data prediction. Updates the
// one-line display and (if open) the upcoming-stops panel.
function updateNextStop() {
  let ns = null, idx = -1;
  const gps = gpsNextStop();
  if (gps) {
    ns = gps.stop; idx = gps.idx;
  } else if (S.predictions[0]) {
    const pr = S.predictions[0];
    const info = S.patternStops?.get(pr.stop_id);
    ns = { stop_id: pr.stop_id, name: pr.stop_name, near_side: info ? info.is_near_side : null };
    idx = idxInPattern(pr.stop_id);
  }
  if (!ns) return;
  S.nextStop = ns;
  S.nextStopIdx = idx;
  renderNextStop();
  if (!$("stops-panel").classList.contains("collapsed")) renderUpcoming();
}

// First stop the phone hasn't passed by more than BUFFER_M, projected onto the
// route shape. Null when GPS is coarse or the pattern geometry isn't bundled.
function gpsNextStop() {
  const p = S.lastPing;
  if (!p || p.accuracy == null || p.accuracy > GPS_GOOD_ACCURACY_M) return null;
  if (!S.patternStopsArr || !S.patternShape) return null;
  const proj = geo.projectOntoShape(p.lat, p.lon, S.patternShape);
  if (!proj) return null;
  const arr = S.patternStopsArr;
  for (let i = 0; i < arr.length; i++) {
    if (arr[i].dist_along_m >= proj.dist_m - BUFFER_M) return { stop: stopOf(arr[i]), idx: i };
  }
  const last = arr.length - 1;
  return last >= 0 ? { stop: stopOf(arr[last]), idx: last } : null;
}

function stopOf(s) {
  return { stop_id: s.stop_id, name: s.name, near_side: s.is_near_side ?? null };
}

function idxInPattern(stopId) {
  return S.patternStopsArr ? S.patternStopsArr.findIndex((s) => s.stop_id === stopId) : -1;
}

function renderNextStop() {
  $("next-stop-name").textContent = S.nextStop?.name || "–";
  const badge = $("next-stop-nearside");
  const nearSide = S.nextStop?.near_side ?? null;
  if (nearSide === true) {
    badge.textContent = "⚠ near-side"; badge.className = "badge";
  } else if (nearSide === false) {
    badge.className = "badge hidden";
  } else {
    badge.textContent = "near-side?"; badge.className = "badge unknown";
  }
}

// Next three stops + … + terminus, from the bundled pattern; ETA per stop when
// the provider supplies one. Falls back to the raw prediction list (e.g. MTA's
// OnwardCalls) when the pattern isn't bundled.
function renderUpcoming() {
  const el = $("upcoming-list");
  let items = [];
  if (S.patternStopsArr && S.nextStopIdx >= 0) {
    const arr = S.patternStopsArr;
    const start = S.nextStopIdx;
    const head = arr.slice(start, start + 3);
    items = head.map((s) => ({ name: s.name, eta: S.etaByStop.get(s.stop_id) }));
    const lastShown = start + head.length - 1;
    if (lastShown < arr.length - 2) items.push({ ellipsis: true });
    if (lastShown < arr.length - 1) {
      const term = arr[arr.length - 1];
      items.push({ name: term.name, eta: S.etaByStop.get(term.stop_id), terminus: true });
    }
  } else if (S.predictions.length) {
    items = S.predictions.slice(0, 4).map((p) => ({ name: p.stop_name, eta: p.eta_min }));
  }

  el.innerHTML = "";
  if (!items.length) {
    el.innerHTML = `<div class="upcoming-empty">No upcoming-stop data yet.</div>`;
    return;
  }
  for (const it of items) {
    const row = document.createElement("div");
    row.className = "upcoming-row" + (it.terminus ? " terminus" : "");
    if (it.ellipsis) {
      row.innerHTML = `<span class="up-name">⋯</span>`;
    } else {
      row.innerHTML =
        `<span class="up-name">${escapeHtml(it.name)}</span>` +
        `<span class="up-eta">${etaText(it.eta)}</span>`;
    }
    el.appendChild(row);
  }
}

// ------------------------------------------------------------ event editor

let editingId = null;
let editorStop = null; // {stop_id, stop_name} override chosen in the picker, or null

function openEditor(eventId) {
  editingId = eventId;
  db.getEvent(eventId).then((e) => {
    if (!e) return;
    const sel = $("editor-type");
    sel.innerHTML = "";
    for (const [type, cfg] of Object.entries(EVENT_TYPES)) {
      const opt = document.createElement("option");
      opt.value = type;
      opt.textContent = cfg.short;
      opt.selected = type === e.type;
      sel.appendChild(opt);
    }
    $("editor-note").value = e.note || "";
    const endLabel = e.type === "note" ? "(note)"
      : (e.end_t ? exp.fmtTime(e.end_t).slice(11, 19) : "active");
    $("editor-times").textContent =
      `${exp.fmtTime(e.start_t).slice(11, 19)} → ${endLabel}` +
      (e.stop_name ? ` · ${e.stop_name}` : "");
    editorStop = null;
    $("editor-stop-input").value = e.stop_name || "";
    $("editor-stop-results").classList.add("hidden");
    $("editor-pax-on").value = e.pax_on || 0;
    $("editor-pax-off").value = e.pax_off || 0;
    updateEditorStopVisibility();
    updateEditorPaxVisibility();
    $("event-editor").classList.remove("hidden");
    $("editor-backdrop").classList.remove("hidden");
  });
}

function updateEditorStopVisibility() {
  const show = STOP_TYPES.has($("editor-type").value) && !S.generic;
  $("editor-stop-field").classList.toggle("hidden", !show);
}

function updateEditorPaxVisibility() {
  $("editor-pax-field").classList.toggle("hidden", $("editor-type").value !== "bus_stop");
}

async function onEditorStopInput() {
  const q = $("editor-stop-input").value.trim().toLowerCase();
  const results = $("editor-stop-results");
  if (q.length < 2) { results.classList.add("hidden"); return; }
  const stops = await ensureAllStops();
  const matches = [];
  for (const s of stops) {
    if (s.name.toLowerCase().includes(q)) {
      matches.push(s);
      if (matches.length >= 25) break;
    }
  }
  renderPickList(results, matches.map((s) => ({
    main: s.name,
    sub: s.stop_id,
    onPick: () => {
      editorStop = { stop_id: s.stop_id, stop_name: s.name };
      $("editor-stop-input").value = s.name;
      results.classList.add("hidden");
    },
  })));
  results.classList.toggle("hidden", !matches.length);
}

async function ensureAllStops() {
  if (!allStopsCache) allStopsCache = await provider.getAllStops();
  return allStopsCache;
}

function closeEditor() {
  editingId = null;
  $("event-editor").classList.add("hidden");
  $("editor-backdrop").classList.add("hidden");
}

async function saveEditor() {
  if (!editingId) return;
  const fields = {
    type: $("editor-type").value,
    note: $("editor-note").value.trim(),
  };
  if (STOP_TYPES.has(fields.type) && editorStop) {
    fields.stop_id = editorStop.stop_id;
    fields.stop_name = editorStop.stop_name;
  }
  if (fields.type === "bus_stop") {
    fields.pax_on = Math.max(0, parseInt($("editor-pax-on").value, 10) || 0);
    fields.pax_off = Math.max(0, parseInt($("editor-pax-off").value, 10) || 0);
  }
  const updated = await db.updateEvent(editingId, fields);
  if (S.activeEvent?.id === editingId) {
    S.activeEvent = updated;
    refreshControls();
  }
  closeEditor();
  await renderEventList();
}

// --------------------------------------------------------- predictions poll

async function pollPredictions() {
  if (!S.meta || !provider) return;
  try {
    const preds = await provider.getNextStops(S.meta.bus_id);
    S.predictions = preds;
    S.etaByStop = new Map(preds.filter((p) => p.eta_min != null).map((p) => [p.stop_id, p.eta_min]));
  } catch {
    /* transient network error — keep last value */
  }
  updateNextStop();
}

// --------------------------------------------------------------- map panel

function initMap() {
  if (S.map) return;
  const cfg = getCityConfig(S.meta.city);
  S.map = new maplibregl.Map({
    container: "map",
    style: {
      version: 8,
      sources: {
        carto: {
          type: "raster",
          tiles: [
            "https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
            "https://b.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
          ],
          tileSize: 256,
          attribution: "© OpenStreetMap, © CARTO",
        },
      },
      layers: [{ id: "carto", type: "raster", source: "carto" }],
    },
    center: cfg.map.center,
    zoom: cfg.map.zoom,
    attributionControl: { compact: true },
  });

  const phoneEl = document.createElement("div");
  phoneEl.style.cssText =
    "width:16px;height:16px;border-radius:50%;background:#1a73e8;border:3px solid #fff;box-shadow:0 0 6px rgba(0,0,0,.4)";
  S.phoneMarker = new maplibregl.Marker({ element: phoneEl });

  const busEl = document.createElement("div");
  busEl.textContent = "🚌";
  busEl.style.cssText = "font-size:24px;line-height:1";
  S.busMarker = new maplibregl.Marker({ element: busEl });
}

function toggleMap() {
  const panel = $("map-panel");
  const collapsed = panel.classList.toggle("collapsed");
  $("btn-map-toggle").classList.toggle("active", !collapsed);
  if (!collapsed) {
    setTimeout(() => S.map?.resize(), 250);
    if (S.generic) updateGenericMap(); else pollVehicle();
  }
}

function toggleUpcoming() {
  const panel = $("stops-panel");
  const collapsed = panel.classList.toggle("collapsed");
  $("btn-stops-toggle").classList.toggle("active", !collapsed);
  if (!collapsed) renderUpcoming();
}

// One getVehicle poll drives two things: (1) keep the pattern current, and
// (2) move the bus marker if the map is open.
//
// (1) is essential: getVehicle at boarding can report the pattern of the trip
// the bus just *finished* (often the opposite direction), and a bus may start
// a new trip mid-recording. A frozen start-time pattern then mismatches every
// stop. So when the live pattern changes we adopt it, reload its stops/shape,
// and update the trip identity that came from the same call.
async function pollVehicle() {
  if (!S.meta || !provider) return;
  let v;
  try {
    v = await provider.getVehicle(S.meta.bus_id);
  } catch {
    return; // vehicle may drop off the tracker between trips
  }

  const pid = String(v.pattern_id || "");
  if (pid && pid !== S.meta.pattern_id) {
    S.meta.pattern_id = pid;
    if (v.trip_id) S.meta.trip_id = v.trip_id;
    if (v.destination) S.meta.destination = v.destination;
    await db.putTripMeta(S.meta);
    S.patternStopsArr = null;
    S.patternStops = null;
    S.patternShape = null;
    loadPattern(pid);
    pollPredictions();
  }

  if (!$("map-panel").classList.contains("collapsed")) {
    if (S.lastPing) S.phoneMarker.setLngLat([S.lastPing.lon, S.lastPing.lat]).addTo(S.map);
    S.busMarker.setLngLat([v.lon, v.lat]).addTo(S.map);
    const anchor = S.lastPing
      ? [(S.lastPing.lon + v.lon) / 2, (S.lastPing.lat + v.lat) / 2]
      : [v.lon, v.lat];
    S.map.easeTo({ center: anchor, duration: 500 });
  }
}

// ============================================================ END / SUMMARY

async function onEndTrip() {
  if (!confirm("End the trip and stop recording?")) return;
  if (S.activeEvent) await setActive(null); // close any open event
  clearTimers();
  stopSensors();
  S.meta.end_t = Date.now();
  S.meta.status = "ended";
  await db.putTripMeta(S.meta);
  const finalSync = sync.isRunning() ? sync.finish() : Promise.resolve(null);
  await showSummary();
  finalSync.then((res) => { if (res) $("summary-sync").textContent = `Server backup: ${res}`; });
}

async function showSummary() {
  showView("summary");
  const meta = S.meta;
  const [events, pings, motionN] = await Promise.all([
    db.getEvents(meta.key), db.countPings(meta.key), db.countMotionSamples(meta.key),
  ]);

  const byType = {};
  for (const e of events) {
    if (e.end_t == null || e.type === "note") continue;
    const agg = (byType[e.type] ??= { n: 0, ms: 0 });
    agg.n += 1; agg.ms += e.end_t - e.start_t;
  }

  const stats = $("summary-stats");
  stats.innerHTML = "";
  const addRow = (label, value) => {
    const row = document.createElement("div");
    row.className = "row";
    row.innerHTML = `<span>${label}</span><strong>${value}</strong>`;
    stats.appendChild(row);
  };
  addRow("City", getCityConfig(meta.city).label);
  addRow("Route / bus / trip", `${meta.route_id} / #${meta.bus_id} / ${meta.trip_id}`);
  addRow("Duration", fmtDur(meta.end_t - meta.start_t));
  addRow("GPS pings", pings);
  addRow("Motion samples", motionN);
  addRow("Events", events.length);
  for (const [type, agg] of Object.entries(byType)) {
    addRow(`· ${(EVENT_TYPES[type] || { short: type }).short}`, `${agg.n}× — ${fmtDur(agg.ms)}`);
  }

  const tid = meta.trip_id || meta.bus_id;
  $("btn-dl-pings").onclick = async () =>
    exp.downloadText(`pings_${tid}.csv`, exp.buildPingsCsv(meta, await db.getPings(meta.key)));
  $("btn-dl-events").onclick = async () =>
    exp.downloadText(`events_${tid}.csv`, exp.buildEventsCsv(meta, await db.getEvents(meta.key)));
  $("btn-dl-motion").onclick = async () =>
    exp.downloadText(`motion_${tid}.csv`, exp.buildMotionCsv(await db.getMotionBatches(meta.key)));
  $("btn-dl-meta").onclick = async () =>
    exp.downloadText(`trip_meta_${tid}.json`,
      exp.buildMetaJson(meta, { pings, motion_samples: motionN, events: events.length }),
      "application/json");
}

// ===================================================== BACKGROUND HANDLING

document.addEventListener("visibilitychange", async () => {
  if (!S.meta || S.meta.status !== "active") return;
  if (document.visibilityState === "hidden") {
    stopSensors();
    S.meta.gaps.push({ start: Date.now(), end: null });
    await db.putTripMeta(S.meta);
  } else {
    const gap = S.meta.gaps.at(-1);
    if (gap && gap.end == null) gap.end = Date.now();
    await db.putTripMeta(S.meta);
    startSensors();
    maybeOfferMotionResume();
  }
});

// iOS only re-grants devicemotion from inside a tap, so after a reload (or
// sometimes a long background) we surface a one-tap pill.
function maybeOfferMotionResume() {
  if (typeof DeviceMotionEvent === "undefined" ||
      typeof DeviceMotionEvent.requestPermission !== "function") return;
  setTimeout(() => {
    if (S.motionWindow.some((t) => t > Date.now() - 3000)) return; // flowing
    if ($("motion-resume")) return;
    const pill = document.createElement("button");
    pill.id = "motion-resume";
    pill.className = "primary";
    pill.textContent = "👆 Tap to re-enable motion sensors";
    pill.onclick = async () => {
      await sensors.requestMotionPermission();
      S.motion?.stop();
      S.motion = sensors.startMotion(onMotionSample);
      pill.remove();
    };
    $("status-bar").insertAdjacentElement("afterend", pill);
  }, 3000);
}

// ================================================================ BOOTSTRAP

async function init() {
  populateCities();
  $("city-select").onchange = onCityChange;
  ensureProvider(); // preload the selected city's provider

  $("observer-name").value = localStorage.getItem("observer_name") || "";
  $("sync-token").value = localStorage.getItem("sync_token") || "";
  $("sync-enabled").checked = localStorage.getItem("sync_enabled") === "1";
  $("sync-token").oninput = (e) => { localStorage.setItem("sync_token", e.target.value.trim()); updateSyncWarn(); };
  $("sync-enabled").onchange = (e) => {
    localStorage.setItem("sync_enabled", e.target.checked ? "1" : "0");
    updateSyncWarn();
  };
  updateSyncWarn();

  // Setup flow
  $("btn-lookup").onclick = onLookup;
  $("vehicle-id").addEventListener("keydown", (e) => { if (e.key === "Enter") onLookup(); });
  $("btn-find-stop").onclick = onFindStop;
  $("btn-stops-back").onclick = () => setupStep("main");
  $("btn-buses-back").onclick = () => setupStep("stops");
  $("btn-confirm-back").onclick = () => { pendingVehicle = null; setupStep("main"); };
  $("btn-start").onclick = onStartTrip;

  // Recording controls
  $("btn-moving").onclick = () => setActive(null);
  $("btn-exit").onclick = () => setActive("exit_wait");
  $("btn-exit").style.background = EVENT_TYPES.exit_wait.color;
  $("btn-note").onclick = addNote;
  $("btn-end").onclick = onEndTrip;
  $("btn-map-toggle").onclick = toggleMap;
  $("btn-stops-toggle").onclick = toggleUpcoming;
  $("btn-turn-left").onclick = () => setTurnDirection("left");
  $("btn-turn-right").onclick = () => setTurnDirection("right");
  $("btn-pax-on").onclick = () => incrementPax("pax_on");
  $("btn-pax-off").onclick = () => incrementPax("pax_off");
  $("btn-edit-active").onclick = () => { if (S.activeEvent) openEditor(S.activeEvent.id); };

  // Editor
  $("editor-type").onchange = () => { updateEditorStopVisibility(); updateEditorPaxVisibility(); };
  $("editor-stop-input").addEventListener("input", onEditorStopInput);
  $("editor-save").onclick = saveEditor;
  $("editor-cancel").onclick = closeEditor;
  $("editor-backdrop").onclick = closeEditor;
  $("btn-new-trip").onclick = () => location.reload();

  // Rehydrate: if a trip is mid-flight (reload, crash, returned later),
  // resume straight into the Recording view with all state intact.
  const active = await db.getActiveTrip();
  if (active) {
    if (!isGeneric(active.city)) provider = await loadProvider(active.city || DEFAULT_CITY);
    const openEvent = await db.getOpenEvent(active.key);
    await beginRecording(active, openEvent);
    maybeOfferMotionResume();
  } else {
    setupStep("main");
    showView("setup");
  }
}

init().catch((err) => {
  console.error(err);
  document.body.insertAdjacentHTML(
    "afterbegin",
    `<pre style="padding:16px;color:#c00;white-space:pre-wrap">${err.stack || err}</pre>`
  );
});
