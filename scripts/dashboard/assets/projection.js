// Polyline ↔ along-route-distance helpers. Pure functions, no DOM / no
// MapLibre — the map view imports these and feeds them its viewport state.

const LATMETER = 111320.0;

// Equirectangular distance between two [lon, lat] pairs in meters. Good
// enough for cursor projection over a route corridor a few km wide.
function distMetersLL(a, b) {
  const dLat = (a[1] - b[1]) * LATMETER;
  const dLon = (a[0] - b[0]) * LATMETER * Math.cos(((a[1] + b[1]) / 2) * Math.PI / 180);
  return Math.hypot(dLat, dLon);
}

// Project a [lon, lat] cursor onto the nearest polyline vertex and return
// that vertex's cumulative distance along the route. Uses a full scan over
// the (~1k vertex) polyline; cheap enough at mousemove frequency.
//
// (We project to the nearest *vertex* rather than the nearest point on a
// segment because the dist-along-route values are anchored to vertices in
// `cumdist_m`. The error is at most half a typical vertex spacing — for
// the CTA GTFS shapes that's <5 m, well under the user's resolution.)
export function projectCursorToRoute(lonlat, polyline_lonlat, cumdist_m) {
  let bestIdx = 0;
  let bestD = Infinity;
  for (let i = 0; i < polyline_lonlat.length; i++) {
    const d = distMetersLL(lonlat, polyline_lonlat[i]);
    if (d < bestD) {
      bestD = d;
      bestIdx = i;
    }
  }
  return { idx: bestIdx, distM: cumdist_m[bestIdx], perpM: bestD };
}

// Compute the route-distance range visible inside the MapLibre viewport.
// Iterate every polyline vertex, project to screen pixels via `map.project`,
// keep those whose pixel coords lie inside the canvas rectangle, and
// return [min, max] of their cumulative distances. Works regardless of
// map rotation.
//
// If no polyline vertex is on screen (heavily zoomed out off-route), we
// fall back to [0, length_m] so the speed profile still shows the full
// route rather than collapsing to an empty domain.
export function visibleRouteRange(map, polyline_lonlat, cumdist_m) {
  const canvas = map.getCanvas();
  const w = canvas.clientWidth;
  const h = canvas.clientHeight;
  let lo = Infinity, hi = -Infinity;
  for (let i = 0; i < polyline_lonlat.length; i++) {
    const p = map.project(polyline_lonlat[i]);
    if (p.x >= 0 && p.x <= w && p.y >= 0 && p.y <= h) {
      const d = cumdist_m[i];
      if (d < lo) lo = d;
      if (d > hi) hi = d;
    }
  }
  if (!isFinite(lo) || !isFinite(hi) || lo === hi) {
    return [0, cumdist_m[cumdist_m.length - 1]];
  }
  return [lo, hi];
}

// Inverse of project: given a target distance, return its [lon, lat] by
// linear interpolation between two adjacent polyline vertices.
export function distToLonLat(distM, polyline_lonlat, cumdist_m) {
  // Binary search for the segment containing distM
  let lo = 0, hi = cumdist_m.length - 1;
  if (distM <= cumdist_m[0]) return polyline_lonlat[0];
  if (distM >= cumdist_m[hi]) return polyline_lonlat[hi];
  while (hi - lo > 1) {
    const mid = (lo + hi) >>> 1;
    if (cumdist_m[mid] <= distM) lo = mid; else hi = mid;
  }
  const span = cumdist_m[hi] - cumdist_m[lo];
  const f = span > 0 ? (distM - cumdist_m[lo]) / span : 0;
  const a = polyline_lonlat[lo];
  const b = polyline_lonlat[hi];
  return [a[0] + f * (b[0] - a[0]), a[1] + f * (b[1] - a[1])];
}
