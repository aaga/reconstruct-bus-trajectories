// PUT/GET /api/trips/<key>/<file...> — upload / download one trip file in
// R2. <file> is a catch-all because motion chunks live at motion/<seq>.csv.
// DELETE /api/trips/<key> (bare key — the catch-all matches zero segments)
// removes the whole trip: every object under trips/<key>/. DELETE with a
// file path removes just that object.
// Object layout: trips/<key>/{meta.json, pings.csv, events.csv,
// motion/0001.csv, ...}

import { CORS, json, authorized, unauthorized } from "../../_utils.js";

const MIME = {
  csv: "text/csv",
  json: "application/json",
};

function objectKey(params) {
  const file = (params.file || []).join("/");
  return { file, key: `trips/${params.key}/${file}` };
}

// Only sane relative paths — no traversal, no empty segments.
function validFile(file) {
  return (
    file.length > 0 &&
    file.length < 200 &&
    /^[A-Za-z0-9._\/-]+$/.test(file) &&
    !file.split("/").some((seg) => seg === "" || seg === "." || seg === "..")
  );
}

// Sane trip keys only ("<epoch-ms>_<trip_id>"), so a whole-trip DELETE can
// never carry a prefix that escapes trips/<key>/.
function validKey(key) {
  return /^[A-Za-z0-9._-]{1,100}$/.test(key);
}

async function deleteTrip(env, tripKey) {
  const prefix = `trips/${tripKey}/`;
  let deleted = 0;
  let cursor;
  do {
    const page = await env.TRIPS.list({ prefix, cursor, limit: 1000 });
    if (page.objects.length) {
      await env.TRIPS.delete(page.objects.map((o) => o.key));
      deleted += page.objects.length;
    }
    cursor = page.truncated ? page.cursor : undefined;
  } while (cursor);
  return deleted;
}

export async function onRequest({ request, env, params }) {
  if (request.method === "OPTIONS") return new Response(null, { headers: CORS });
  if (!authorized(request, env)) return unauthorized();
  if (!env.TRIPS) return json({ error: "TRIPS R2 binding not configured" }, 500);

  const { file, key } = objectKey(params);

  // Whole-trip delete: bare /api/trips/<key> with no file path.
  if (request.method === "DELETE" && file === "") {
    if (!validKey(params.key)) return json({ error: "bad trip key" }, 400);
    const deleted = await deleteTrip(env, params.key);
    if (!deleted) return json({ error: "not found" }, 404);
    return json({ ok: true, deleted });
  }

  if (!validFile(file)) return json({ error: "bad file path" }, 400);
  const ext = file.split(".").pop();

  if (request.method === "PUT") {
    await env.TRIPS.put(key, request.body, {
      httpMetadata: {
        contentType: request.headers.get("Content-Type") || MIME[ext] || "application/octet-stream",
      },
    });
    return json({ ok: true, key });
  }

  if (request.method === "DELETE") {
    await env.TRIPS.delete(key);
    return json({ ok: true, deleted: 1 });
  }

  if (request.method === "GET") {
    const obj = await env.TRIPS.get(key);
    if (!obj) return json({ error: "not found" }, 404);
    const headers = new Headers(CORS);
    headers.set("Content-Type", obj.httpMetadata?.contentType || MIME[ext] || "application/octet-stream");
    if (new URL(request.url).searchParams.get("dl") === "1") {
      headers.set("Content-Disposition", `attachment; filename="${file.replace(/\//g, "_")}"`);
    }
    return new Response(obj.body, { headers });
  }

  return json({ error: "method not allowed" }, 405);
}
