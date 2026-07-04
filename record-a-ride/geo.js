// Pure geographic helpers shared by the providers and the controller. No
// agency knowledge lives here — just distance and the GPS-to-route projection
// the Recording view uses to decide the next stop from the phone's own fix.

const R_EARTH_M = 6371000;

export function haversineM(lat1, lon1, lat2, lon2) {
  const rad = Math.PI / 180;
  const dLat = (lat2 - lat1) * rad;
  const dLon = (lon2 - lon1) * rad;
  const a =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(lat1 * rad) * Math.cos(lat2 * rad) * Math.sin(dLon / 2) ** 2;
  return 2 * R_EARTH_M * Math.asin(Math.sqrt(a));
}

// Local equirectangular metres-per-degree at a latitude. Good enough for the
// few-km spans we project over, and far cheaper than haversine per segment.
function metresPerDeg(lat) {
  const rad = Math.PI / 180;
  return { x: Math.cos(lat * rad) * 111320, y: 110540 };
}

/**
 * Project (lat, lon) onto a pattern shape and return how far along the route
 * that projection lands.
 *
 * `shape` is [[lon, lat, cumDist_m], ...] — the cumulative distance lets us
 * read the along-route metre offset straight off the matched segment. Returns
 * { dist_m, offset_m } where dist_m is the along-route distance of the nearest
 * point and offset_m is the perpendicular distance from the route (a rough
 * confidence signal). Returns null for an empty/too-short shape.
 */
export function projectOntoShape(lat, lon, shape) {
  if (!shape || shape.length < 2) return null;
  const m = metresPerDeg(lat);
  const px = lon * m.x;
  const py = lat * m.y;

  let best = null;
  for (let i = 0; i < shape.length - 1; i++) {
    const a = shape[i];
    const b = shape[i + 1];
    const ax = a[0] * m.x, ay = a[1] * m.y;
    const bx = b[0] * m.x, by = b[1] * m.y;
    const dx = bx - ax, dy = by - ay;
    const segLen2 = dx * dx + dy * dy;
    let t = segLen2 ? ((px - ax) * dx + (py - ay) * dy) / segLen2 : 0;
    t = Math.max(0, Math.min(1, t));
    const cx = ax + t * dx, cy = ay + t * dy;
    const off2 = (px - cx) ** 2 + (py - cy) ** 2;
    if (best === null || off2 < best.off2) {
      const segM = Math.sqrt(segLen2);
      const along = a[2] + t * segM; // cumDist at A + distance into the segment
      best = { off2, dist_m: along };
    }
  }
  return best ? { dist_m: best.dist_m, offset_m: Math.sqrt(best.off2) } : null;
}
