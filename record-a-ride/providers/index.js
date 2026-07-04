// City registry — the single source of truth for "which cities exist". Each
// entry carries the UI-facing config (label, map view, vehicle-input hint) and
// a lazy loader for its provider module. Adding a city = one entry here, a
// providers/<city>.js implementing the provider interface, and its data
// bundles. Nothing else in the app hardcodes a city.
//
// Provider interface (every providers/<city>.js exports exactly these):
//   getVehicle(busId)          -> Vehicle           (throws ProviderError if not found)
//   getNextStops(busId)        -> StopPrediction[]  (stops ahead of the bus, ETA source/fallback)
//   getPredictions(stopId)     -> StopPrediction[]  (buses approaching a stop)
//   getPatternStops(patternId) -> PatternStop[] | null
//   getPatternShape(patternId) -> [[lon,lat,cumDist_m], ...] | null
//   getNearbyStops(lat,lon,n)  -> NearbyStop[]
//   getAllStops()              -> [{stop_id, name, lat, lon}]  (for the editor stop picker)
//   validateVehicleId(raw)     -> normalized id string | null
//   ProviderError              (Error subclass)
//
// Normalized shapes:
//   Vehicle        { bus_id, lat, lon, pattern_id, route_id, destination, trip_id }
//   StopPrediction { stop_id, stop_name, eta_min, vehicle_id, trip_id, route_id, destination }
//   NearbyStop     { stop_id, name, lat, lon, dist_m }
//   PatternStop    { stop_id, name, lat, lon, dist_along_m, is_near_side }

export const CITIES = {
  cta: {
    id: "cta",
    label: "Chicago (CTA)",
    map: { center: [-87.63, 41.88], zoom: 14 },
    vehiclePlaceholder: "e.g. 1809",
    load: () => import("./cta.js"),
  },
  mta: {
    id: "mta",
    label: "New York (MTA Bus)",
    map: { center: [-73.97, 40.75], zoom: 13 },
    vehiclePlaceholder: "e.g. 4567",
    load: () => import("./mta.js"),
  },
};

export const DEFAULT_CITY = "cta";

export function getCityConfig(id) {
  return CITIES[id] || CITIES[DEFAULT_CITY];
}

/** Resolve a city id to its loaded provider module (dynamic import). */
export async function loadProvider(id) {
  return getCityConfig(id).load();
}
