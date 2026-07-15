// Shared helpers for the trips API. Files prefixed with "_" are not routed
// by Cloudflare Pages Functions.

export const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, PUT, DELETE, OPTIONS",
  "Access-Control-Allow-Headers": "Authorization, Content-Type",
};

export function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", ...CORS },
  });
}

/**
 * Trip uploads/downloads are gated by a single shared secret (SYNC_TOKEN
 * env var). Accepted as `Authorization: Bearer <token>` or as a `?token=`
 * query param — the latter so plain <a href> download links work from the
 * desktop trips browser.
 */
export function authorized(request, env) {
  if (!env.SYNC_TOKEN) return false; // unset token = locked, not open
  const header = request.headers.get("Authorization") || "";
  if (header === `Bearer ${env.SYNC_TOKEN}`) return true;
  const token = new URL(request.url).searchParams.get("token");
  return token === env.SYNC_TOKEN;
}

export function unauthorized() {
  return json({ error: "missing or invalid sync token" }, 401);
}
