// IndexedDB persistence for the observation tool. Everything the app knows
// lives here, so a page reload (or returning from the background) can
// rehydrate mid-trip without losing anything.
//
// Stores:
//   tripMeta — keyPath "key". One record per recorded trip.
//              `key` is a locally-unique id ("<start ts>_<trip id>");
//              `trip_id` is the agency trip id that goes into the CSVs;
//              `city` selects the transit provider (defaults to DEFAULT_CITY
//              for records written before multi-city support).
//              status: "active" | "ended".
//   pings    — autoincrement; index by_key. ~1 Hz GPS fixes:
//              { key, t, lat, lon, accuracy, heading, speed }
//   motion   — autoincrement; index by_key. Batched devicemotion samples —
//              one record holds ~2 s of samples so we don't write to
//              IndexedDB at 60 Hz: { key, n, samples: [{t, interval_ms,
//              ax, ay, az, gx, gy, gz}, ...] }
//   events   — keyPath "id" (uuid); index by_key. Delay-event intervals
//              (a "note" event is instantaneous: end_t === start_t):
//              { id, key, trip_id, type, note, start_t, end_t, start_lat,
//                start_lon, end_lat, end_lon, parent_id, direction,
//                stop_id, stop_name, near_side }
//
// Timestamps are epoch milliseconds throughout; export.js owns the
// pipeline-facing "YYYY-MM-DD HH:MM:SS.ffffff" formatting.

const DB_NAME = "obs_tool";
const DB_VERSION = 1;

let _dbPromise = null;

function db() {
  if (_dbPromise) return _dbPromise;
  _dbPromise = new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      const d = req.result;
      d.createObjectStore("tripMeta", { keyPath: "key" });
      const pings = d.createObjectStore("pings", { autoIncrement: true });
      pings.createIndex("by_key", "key");
      const motion = d.createObjectStore("motion", { autoIncrement: true });
      motion.createIndex("by_key", "key");
      const events = d.createObjectStore("events", { keyPath: "id" });
      events.createIndex("by_key", "key");
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
  return _dbPromise;
}

function promisify(req) {
  return new Promise((resolve, reject) => {
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function store(name, mode = "readonly") {
  return (await db()).transaction(name, mode).objectStore(name);
}

// ---------------------------------------------------------------- tripMeta

export async function putTripMeta(meta) {
  return promisify((await store("tripMeta", "readwrite")).put(meta));
}

export async function getTripMeta(key) {
  return promisify((await store("tripMeta")).get(key));
}

export async function listTrips() {
  return promisify((await store("tripMeta")).getAll());
}

/** The trip a reload should resume into, if any (most recent active one). */
export async function getActiveTrip() {
  const all = await listTrips();
  const active = all.filter((m) => m.status === "active");
  active.sort((a, b) => b.start_t - a.start_t);
  return active[0] || null;
}

// ------------------------------------------------------------------ pings

export async function addPing(ping) {
  return promisify((await store("pings", "readwrite")).add(ping));
}

export async function getPings(key) {
  const s = await store("pings");
  const rows = await promisify(s.index("by_key").getAll(IDBKeyRange.only(key)));
  rows.sort((a, b) => a.t - b.t);
  return rows;
}

export async function countPings(key) {
  const s = await store("pings");
  return promisify(s.index("by_key").count(IDBKeyRange.only(key)));
}

// ----------------------------------------------------------------- motion

export async function addMotionBatch(key, samples) {
  if (!samples.length) return;
  const s = await store("motion", "readwrite");
  return promisify(s.add({ key, n: samples.length, samples }));
}

export async function getMotionBatches(key) {
  const s = await store("motion");
  const rows = await promisify(s.index("by_key").getAll(IDBKeyRange.only(key)));
  rows.sort((a, b) => a.samples[0]?.t - b.samples[0]?.t);
  return rows;
}

export async function countMotionSamples(key) {
  const batches = await getMotionBatches(key);
  return batches.reduce((acc, b) => acc + b.n, 0);
}

// ----------------------------------------------------------------- events

export async function putEvent(event) {
  return promisify((await store("events", "readwrite")).put(event));
}

export async function getEvent(id) {
  return promisify((await store("events")).get(id));
}

/** Partial update; merges fields into the stored record. */
export async function updateEvent(id, fields) {
  const existing = await getEvent(id);
  if (!existing) throw new Error(`event ${id} not found`);
  const merged = { ...existing, ...fields };
  await putEvent(merged);
  return merged;
}

export async function getEvents(key) {
  const s = await store("events");
  const rows = await promisify(s.index("by_key").getAll(IDBKeyRange.only(key)));
  rows.sort((a, b) => a.start_t - b.start_t);
  return rows;
}

/** The currently-open (end_t === null) event for a trip, if any. */
export async function getOpenEvent(key) {
  const rows = await getEvents(key);
  return rows.find((e) => e.end_t === null) || null;
}

// ------------------------------------------------------------ maintenance

export async function deleteTrip(key) {
  const d = await db();
  const tx = d.transaction(["tripMeta", "pings", "motion", "events"], "readwrite");
  tx.objectStore("tripMeta").delete(key);
  for (const name of ["pings", "motion", "events"]) {
    const idx = tx.objectStore(name).index("by_key");
    const req = idx.openCursor(IDBKeyRange.only(key));
    req.onsuccess = () => {
      const cursor = req.result;
      if (cursor) {
        cursor.delete();
        cursor.continue();
      }
    };
  }
  return new Promise((resolve, reject) => {
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}
