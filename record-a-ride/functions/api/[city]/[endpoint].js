// Per-city live-data proxy (Cloudflare Pages Function).
//
// The upstream transit APIs (CTA BusTime, MTA Bus Time SIRI) have no CORS and
// require a secret key, so the phone hits /api/<city>/<endpoint>?... and we
// forward to the city's upstream with the key from <CITY>_KEY injected
// server-side. Responses are edge-cached a few seconds — predictions churn
// faster than that anyway, and it keeps a bus full of curious riders from
// burning the API quota. Unknown cities/endpoints are rejected so the proxy
// can't be repurposed as a general key oracle.

import { UPSTREAMS } from "../_providers.js";

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
};

const json = (obj, status) =>
  new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json", ...CORS },
  });

export async function onRequest({ request, env, params }) {
  if (request.method === "OPTIONS") return new Response(null, { headers: CORS });
  if (request.method !== "GET") {
    return new Response("method not allowed", { status: 405, headers: CORS });
  }

  const cfg = UPSTREAMS[params.city];
  if (!cfg) return json({ error: `unknown city ${params.city}` }, 404);

  const endpoint = params.endpoint || "";
  if (!cfg.allowed.has(endpoint)) {
    return json({ error: `unknown endpoint ${endpoint}` }, 404);
  }
  if (!env[cfg.keyEnv]) {
    return json({ error: `${cfg.keyEnv} not configured` }, 500);
  }

  const upstream = new URL(`${cfg.base}/${endpoint}`);
  for (const [k, v] of new URL(request.url).searchParams) {
    if (k !== "key" && k !== "format") upstream.searchParams.set(k, v);
  }
  for (const [k, v] of Object.entries(cfg.extra)) upstream.searchParams.set(k, v);
  upstream.searchParams.set(cfg.keyParam, env[cfg.keyEnv]);

  const resp = await fetch(upstream.toString(), {
    cf: { cacheTtl: cfg.cacheTtl, cacheEverything: true },
  });
  const body = await resp.text();
  return new Response(body, {
    status: resp.status,
    headers: {
      "Content-Type": "application/json",
      "Cache-Control": `public, max-age=${cfg.cacheTtl}`,
      ...CORS,
    },
  });
}
