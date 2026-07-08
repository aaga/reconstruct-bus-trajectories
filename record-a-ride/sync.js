// Opt-in server backup. Every 60 s, snapshot the trip's small files
// (meta.json, pings.csv, events.csv) — these are cheap to
// rebuild and overwrite — and upload only the NEW motion batches as an
// append-only chunk (motion/<seq>.csv), since motion data is MB-scale.
//
// The desktop trips.html browser reassembles chunks on download. Auth is a
// single shared bearer token (SYNC_TOKEN on the server), kept in
// localStorage after the observer enters it once in Setup.

import * as db from "./storage.js";
import * as exp from "./export.js";

const INTERVAL_MS = 60000;

let _timer = null;
let _key = null;
let _onStatus = null;
let _uploadedBatches = 0; // motion batches already shipped
let _chunkSeq = 0;
let _busy = false;

function authHeaders() {
  const token = localStorage.getItem("sync_token") || "";
  return token ? { Authorization: `Bearer ${token}` } : {};
}

async function put(file, body, mime) {
  const resp = await fetch(`/api/trips/${encodeURIComponent(_key)}/${file}`, {
    method: "PUT",
    headers: { "Content-Type": mime, ...authHeaders() },
    body,
  });
  if (!resp.ok) throw new Error(`PUT ${file}: HTTP ${resp.status}`);
}

async function syncOnce() {
  if (_busy || !_key) return;
  _busy = true;
  try {
    const meta = await db.getTripMeta(_key);
    if (!meta) return;
    const [pings, events, batches, motionN] = await Promise.all([
      db.getPings(_key),
      db.getEvents(_key),
      db.getMotionBatches(_key),
      db.countMotionSamples(_key),
    ]);

    await put("meta.json",
      exp.buildMetaJson(meta, { pings: pings.length, events: events.length, motion_samples: motionN }),
      "application/json");
    await put("pings.csv", exp.buildPingsCsv(meta, pings), "text/csv");
    await put("events.csv", exp.buildEventsCsv(meta, events), "text/csv");

    // Append-only motion chunks: only batches we haven't shipped yet.
    const fresh = batches.slice(_uploadedBatches);
    if (fresh.length) {
      _chunkSeq += 1;
      const name = `motion/${String(_chunkSeq).padStart(4, "0")}.csv`;
      // Header only on the first chunk so concatenation yields one valid CSV.
      await put(name, exp.buildMotionCsv(fresh, { header: _chunkSeq === 1 }), "text/csv");
      _uploadedBatches = batches.length;
    }

    _onStatus?.(`✓ ${new Date().toTimeString().slice(0, 5)}`);
  } catch (err) {
    _onStatus?.("sync ✗");
    console.warn("sync failed:", err);
  } finally {
    _busy = false;
  }
}

export function start(tripKey, onStatus) {
  stop();
  _key = tripKey;
  _onStatus = onStatus;
  _uploadedBatches = 0;
  _chunkSeq = 0;
  _onStatus?.("sync…");
  syncOnce();
  _timer = setInterval(syncOnce, INTERVAL_MS);
}

export function stop() {
  if (_timer) clearInterval(_timer);
  _timer = null;
}

export function isRunning() {
  return _timer != null;
}

/** Final flush at trip end; resolves to a short status string. */
export async function finish() {
  stop();
  try {
    await syncOnce();
    return "complete ✓";
  } catch {
    return "final upload failed";
  } finally {
    _key = null;
  }
}
