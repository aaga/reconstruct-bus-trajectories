// PUT/GET /api/trips/<key>/<file...> — upload / download one trip file in
// R2. <file> is a catch-all because motion chunks live at motion/<seq>.csv.
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

export async function onRequest({ request, env, params }) {
  if (request.method === "OPTIONS") return new Response(null, { headers: CORS });
  if (!authorized(request, env)) return unauthorized();
  if (!env.TRIPS) return json({ error: "TRIPS R2 binding not configured" }, 500);

  const { file, key } = objectKey(params);
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
