// GET /api/trips — list every saved trip in the R2 bucket (TRIPS binding),
// grouped by trip key, for the desktop trips.html browser.

import { CORS, json, authorized, unauthorized } from "../_utils.js";

export async function onRequest({ request, env }) {
  if (request.method === "OPTIONS") return new Response(null, { headers: CORS });
  if (request.method !== "GET") return json({ error: "method not allowed" }, 405);
  if (!authorized(request, env)) return unauthorized();
  if (!env.TRIPS) return json({ error: "TRIPS R2 binding not configured" }, 500);

  const trips = new Map(); // key -> { key, files: [] }
  let cursor;
  do {
    const page = await env.TRIPS.list({ prefix: "trips/", cursor, limit: 1000 });
    for (const obj of page.objects) {
      // trips/<key>/<file...>
      const rest = obj.key.slice("trips/".length);
      const slash = rest.indexOf("/");
      if (slash < 0) continue;
      const key = rest.slice(0, slash);
      const file = rest.slice(slash + 1);
      if (!trips.has(key)) trips.set(key, { key, files: [] });
      trips.get(key).files.push({
        name: file,
        size: obj.size,
        uploaded: obj.uploaded,
      });
    }
    cursor = page.truncated ? page.cursor : undefined;
  } while (cursor);

  const out = [...trips.values()].sort((a, b) => b.key.localeCompare(a.key));
  return json({ trips: out });
}
