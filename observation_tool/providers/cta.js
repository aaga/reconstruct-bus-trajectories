// CTA Bus Tracker provider. All live calls go to OUR Pages Function proxy
// (/api/cta/*) — BusTime has no CORS and needs a secret key, so the browser
// can never talk to it directly. Nearby-stop search and the pattern bundles
// are fully offline against data built by scripts/build_stops.py.
//
// This module normalizes every BusTime field into the shapes documented in
// providers/index.js, so app.js never sees `vid`/`pid`/`stpnm`/`prdctdn`.

import { haversineM } from "../geo.js";

const API_BASE = "/api/cta";

export class ProviderError extends Error {}

async function call(endpoint, params) {
  const qs = new URLSearchParams(params).toString();
  const resp = await fetch(`${API_BASE}/${endpoint}?${qs}`);
  if (!resp.ok) throw new Error(`CTA proxy HTTP ${resp.status}`);
  const body = await resp.json();
  const data = body["bustime-response"];
  if (!data) throw new Error("Malformed BusTime response");
  if (data.error?.length) {
    // BusTime reports "no service scheduled" etc. as errors; surface the text.
    throw new ProviderError(data.error.map((e) => e.msg).join("; "));
  }
  return data;
}

// BusTime countdown -> integer minutes. "DUE" means imminent (0); blanks/NaN
// become null so the UI can hide an absent ETA.
function etaMin(prdctdn) {
  if (prdctdn == null || prdctdn === "") return null;
  if (String(prdctdn).toUpperCase() === "DUE") return 0;
  const n = parseInt(prdctdn, 10);
  return Number.isFinite(n) ? n : null;
}

function toPrediction(prd) {
  return {
    stop_id: String(prd.stpid),
    stop_name: prd.stpnm || "",
    eta_min: etaMin(prd.prdctdn),
    vehicle_id: String(prd.vid || ""),
    trip_id: String(prd.tatripid || ""),
    route_id: String(prd.rt || ""),
    destination: prd.des || "",
  };
}

// --------------------------------------------------------- offline bundles

let _stopsPromise = null;
function loadStops() {
  if (!_stopsPromise) {
    _stopsPromise = fetch("./data/cta_bus_stops.json").then((r) => {
      if (!r.ok) throw new Error("cta_bus_stops.json missing — run scripts/build_stops.py");
      return r.json();
    });
  }
  return _stopsPromise;
}

let _patternStopsPromise = null;
function loadPatternStops() {
  if (!_patternStopsPromise) {
    _patternStopsPromise = fetch("./data/cta_pattern_stops.json")
      .then((r) => (r.ok ? r.json() : {}))
      .catch(() => ({}));
  }
  return _patternStopsPromise;
}

let _patternShapesPromise = null;
function loadPatternShapes() {
  if (!_patternShapesPromise) {
    _patternShapesPromise = fetch("./data/cta_pattern_shapes.json")
      .then((r) => (r.ok ? r.json() : {}))
      .catch(() => ({}));
  }
  return _patternShapesPromise;
}

// --------------------------------------------------------------- interface

/** Closest `n` bus stops to (lat, lon), each normalized + dist_m attached. */
export async function getNearbyStops(lat, lon, n = 10) {
  const stops = await loadStops();
  const dDeg = 0.02; // ~2 km box prefilter before exact haversine
  const near = stops.filter(
    (s) => Math.abs(s.lat - lat) < dDeg && Math.abs(s.lon - lon) < dDeg * 1.4
  );
  const pool = near.length >= n ? near : stops;
  return pool
    .map((s) => ({
      stop_id: String(s.stpid),
      name: s.name,
      lat: s.lat,
      lon: s.lon,
      dist_m: haversineM(lat, lon, s.lat, s.lon),
    }))
    .sort((a, b) => a.dist_m - b.dist_m)
    .slice(0, n);
}

/** Every bus stop in the city, normalized: [{stop_id, name, lat, lon}]. */
export async function getAllStops() {
  const stops = await loadStops();
  return stops.map((s) => ({
    stop_id: String(s.stpid),
    name: s.name,
    lat: s.lat,
    lon: s.lon,
  }));
}

/** Ordered PatternStop[] for one pattern, or null when unbundled. */
export async function getPatternStops(patternId) {
  const all = await loadPatternStops();
  const rows = all[String(patternId)];
  if (!rows) return null;
  return rows.map((s) => ({
    stop_id: String(s.stpid),
    name: s.name,
    lat: s.lat ?? null,
    lon: s.lon ?? null,
    dist_along_m: s.dist_along_m,
    is_near_side: s.is_near_side ?? null,
  }));
}

/** Shape polyline [[lon,lat,cumDist_m], ...] for one pattern, or null. */
export async function getPatternShape(patternId) {
  const all = await loadPatternShapes();
  return all[String(patternId)] || null;
}

/** Upcoming stops for the bus we're riding (ETA source / GPS fallback). */
export async function getNextStops(vid) {
  const data = await call("getpredictions", { vid, top: 8 });
  return (data.prd || []).map(toPrediction);
}

/** Buses approaching a stop (the nearby-stop start flow). */
export async function getPredictions(stopId) {
  const data = await call("getpredictions", { stpid: stopId, top: 10 });
  return (data.prd || []).map(toPrediction);
}

/** Live normalized Vehicle for a fleet number. */
export async function getVehicle(busId) {
  const data = await call("getvehicles", { vid: busId });
  const v = (data.vehicle || [])[0];
  if (!v) throw new ProviderError(`vehicle ${busId} not found`);
  return {
    bus_id: String(v.vid || busId),
    lat: Number(v.lat),
    lon: Number(v.lon),
    pattern_id: String(v.pid || ""),
    route_id: String(v.rt || ""),
    destination: v.des || "",
    trip_id: String(v.tatripid || ""),
  };
}

/** CTA vehicle numbers are 1–5 digits. Returns the cleaned id or null. */
export function validateVehicleId(raw) {
  const v = String(raw || "").trim();
  return /^\d{1,5}$/.test(v) ? v : null;
}
