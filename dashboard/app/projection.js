// Polyline ↔ along-route-distance helpers, ported verbatim from
// scripts/dashboard/assets/projection.js. Pure functions, no DOM / MapLibre.

const LATMETER = 111320.0;

function distMetersLL(a, b) {
  const dLat = (a[1] - b[1]) * LATMETER;
  const dLon = (a[0] - b[0]) * LATMETER * Math.cos(((a[1] + b[1]) / 2) * Math.PI / 180);
  return Math.hypot(dLat, dLon);
}

export function projectCursorToRoute(lonlat, polyline_lonlat, cumdist_m) {
  let bestIdx = 0;
  let bestD = Infinity;
  for (let i = 0; i < polyline_lonlat.length; i++) {
    const d = distMetersLL(lonlat, polyline_lonlat[i]);
    if (d < bestD) { bestD = d; bestIdx = i; }
  }
  return { idx: bestIdx, distM: cumdist_m[bestIdx], perpM: bestD };
}

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

export function headingAtDistDeg(distM, polyline_lonlat, cumdist_m) {
  const n = cumdist_m.length;
  if (n < 2) return 0;
  let lo, hi;
  if (distM <= cumdist_m[0]) { lo = 0; hi = 1; }
  else if (distM >= cumdist_m[n - 1]) { lo = n - 2; hi = n - 1; }
  else {
    lo = 0; hi = n - 1;
    while (hi - lo > 1) {
      const mid = (lo + hi) >>> 1;
      if (cumdist_m[mid] <= distM) lo = mid; else hi = mid;
    }
  }
  const [lonA, latA] = polyline_lonlat[lo];
  const [lonB, latB] = polyline_lonlat[hi];
  const mlat = Math.cos((latA + latB) / 2 * Math.PI / 180);
  const dLon = (lonB - lonA) * mlat;
  const dLat = latB - latA;
  const rad = Math.atan2(dLon, dLat);
  return (rad * 180 / Math.PI + 360) % 360;
}

export function distToLonLat(distM, polyline_lonlat, cumdist_m) {
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
