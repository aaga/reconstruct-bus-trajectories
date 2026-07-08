// New York MTA Bus Time provider (SIRI, OneBusAway-NYC backend). Live calls go
// to our Pages Function proxy (/api/mta/*), which injects MTA_KEY. Nearby-stop
// search and the pattern bundles are offline, built by scripts/build_stops.py
// --city mta. Every SIRI field is normalized into the shapes documented in
// providers/index.js so app.js never sees SIRI's nesting.
//
// MTA ref formats (validated live against the SIRI API):
//   VehicleRef   "MTA NYCT_7356" — lookup needs the FULL ref; a bare fleet
//                  number returns nothing, so getVehicle rebuilds the ref by
//                  trying each operator prefix ("MTA NYCT_", then "MTABC_").
//   LineRef      "MTA NYCT_B46"  — route is the part after the last "_".
//   StopPointRef "MTA_303676"    — GTFS stop_id is the bare number; we strip the
//                  prefix to match our bundles and re-add it for stop-monitoring.

import { haversineM } from "../geo.js";

const API_BASE = "/api/mta";
const OPERATOR = "MTA";               // OperatorRef on every call
const STOP_PREFIX = "MTA_";
// Vehicle-ref operator prefixes, most-common first: NYC Transit, then MTA Bus Co.
const VEHICLE_OPERATORS = ["MTA NYCT", "MTABC"];

export class ProviderError extends Error {}

async function siri(endpoint, params) {
  const qs = new URLSearchParams(params).toString();
  const resp = await fetch(`${API_BASE}/${endpoint}?${qs}`);
  if (!resp.ok) throw new Error(`MTA proxy HTTP ${resp.status}`);
  const body = await resp.json();
  const sd = body?.Siri?.ServiceDelivery;
  if (!sd) throw new Error("Malformed SIRI response");
  return sd;
}

// SIRI sometimes wraps string fields in a single-element array.
const first = (v) => (Array.isArray(v) ? v[0] : v) ?? "";
// Drop the operator/agency prefix: "MTA NYCT_B63" -> "B63", "MTA NYCT_5678" -> "5678".
const stripPrefix = (ref) => String(ref || "").split("_").pop();
const stripStop = (ref) => String(ref || "").replace(/^MTA[^_]*_/, "");

// ISO arrival time -> integer minutes from now (0 = due), or null.
function etaFromIso(iso) {
  if (!iso) return null;
  const ms = Date.parse(iso);
  if (!Number.isFinite(ms)) return null;
  return Math.max(0, Math.round((ms - Date.now()) / 60000));
}

function routeOf(mvj) {
  return String(first(mvj.PublishedLineName) || stripPrefix(mvj.LineRef) || "");
}

function patternIdOf(mvj) {
  return `${routeOf(mvj)}_${mvj.DirectionRef ?? ""}`;
}

function callToPrediction(call, mvj) {
  return {
    stop_id: stripStop(call.StopPointRef),
    stop_name: String(first(call.StopPointName) || ""),
    eta_min: etaFromIso(call.ExpectedArrivalTime || call.AimedArrivalTime),
    vehicle_id: stripPrefix(mvj.VehicleRef),
    trip_id: String(mvj.FramedVehicleJourneyRef?.DatedVehicleJourneyRef || ""),
    route_id: routeOf(mvj),
    destination: String(first(mvj.DestinationName) || ""),
  };
}

// Full VehicleRefs to try for a user-entered id: an already-qualified ref as-is,
// otherwise the bare fleet number under each operator prefix.
function candidateRefs(fleet) {
  const s = String(fleet);
  if (s.includes("_")) return [s];
  return VEHICLE_OPERATORS.map((op) => `${op}_${s}`);
}

// The MonitoredVehicleJourney for one fleet number (with onward calls), or null.
// SIRI needs the fully-qualified VehicleRef, so we try each operator prefix.
async function fetchJourney(fleet) {
  for (const ref of candidateRefs(fleet)) {
    const sd = await siri("vehicle-monitoring.json", {
      VehicleRef: ref,
      OperatorRef: OPERATOR,
      VehicleMonitoringDetailLevel: "calls",
    });
    const mvj = sd.VehicleMonitoringDelivery?.[0]?.VehicleActivity?.[0]?.MonitoredVehicleJourney;
    if (mvj) return mvj;
  }
  return null;
}

// --------------------------------------------------------- offline bundles

let _stopsPromise = null;
function loadStops() {
  if (!_stopsPromise) {
    _stopsPromise = fetch("./data/mta_bus_stops.json").then((r) => {
      if (!r.ok) throw new Error("mta_bus_stops.json missing — run scripts/build_stops.py --city mta");
      return r.json();
    });
  }
  return _stopsPromise;
}

let _patternStopsPromise = null;
function loadPatternStops() {
  if (!_patternStopsPromise) {
    _patternStopsPromise = fetch("./data/mta_pattern_stops.json")
      .then((r) => (r.ok ? r.json() : {}))
      .catch(() => ({}));
  }
  return _patternStopsPromise;
}

let _patternShapesPromise = null;
function loadPatternShapes() {
  if (!_patternShapesPromise) {
    _patternShapesPromise = fetch("./data/mta_pattern_shapes.json")
      .then((r) => (r.ok ? r.json() : {}))
      .catch(() => ({}));
  }
  return _patternShapesPromise;
}

// --------------------------------------------------------------- interface

export async function getNearbyStops(lat, lon, n = 10) {
  const stops = await loadStops();
  const dDeg = 0.02;
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

export async function getAllStops() {
  const stops = await loadStops();
  return stops.map((s) => ({ stop_id: String(s.stpid), name: s.name, lat: s.lat, lon: s.lon }));
}

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

export async function getPatternShape(patternId) {
  const all = await loadPatternShapes();
  return all[String(patternId)] || null;
}

/** Upcoming stops for the bus we're riding, from its OnwardCalls. */
export async function getNextStops(busId) {
  const mvj = await fetchJourney(busId);
  if (!mvj) return [];
  const calls = mvj.OnwardCalls?.OnwardCall || [];
  // Fall back to the single MonitoredCall if onward calls came back empty.
  const list = calls.length ? calls : (mvj.MonitoredCall ? [mvj.MonitoredCall] : []);
  return list.map((c) => callToPrediction(c, mvj));
}

/** Buses approaching a stop (the nearby-stop start flow). */
export async function getPredictions(stopId) {
  const sd = await siri("stop-monitoring.json", {
    MonitoringRef: STOP_PREFIX + stripStop(stopId),
    OperatorRef: OPERATOR,
  });
  const visits = sd.StopMonitoringDelivery?.[0]?.MonitoredStopVisit || [];
  return visits.map((v) => {
    const mvj = v.MonitoredVehicleJourney || {};
    return callToPrediction(mvj.MonitoredCall || {}, mvj);
  });
}

/** Live normalized Vehicle for a fleet number. */
export async function getVehicle(busId) {
  const mvj = await fetchJourney(busId);
  if (!mvj) throw new ProviderError(`vehicle ${busId} not found or not in service`);
  const loc = mvj.VehicleLocation || {};
  return {
    bus_id: stripPrefix(mvj.VehicleRef) || String(busId),
    lat: Number(loc.Latitude),
    lon: Number(loc.Longitude),
    pattern_id: patternIdOf(mvj),
    route_id: routeOf(mvj),
    destination: String(first(mvj.DestinationName) || ""),
    trip_id: String(mvj.FramedVehicleJourneyRef?.DatedVehicleJourneyRef || ""),
  };
}

/** MTA fleet numbers are digits, optionally with an operator prefix we drop. */
export function validateVehicleId(raw) {
  const v = stripPrefix(String(raw || "").trim());
  return /^\d{1,7}$/.test(v) ? v : null;
}
