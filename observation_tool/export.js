// CSV / JSON builders + download helpers. This module is the ONE place that
// knows the output file shapes — app.js (end-of-trip export), sync.js
// (server autosave) and trips.js (desktop rebuild) all import from here so
// the formats can never drift apart.
//
// pings_<trip_id>.csv is shaped for src/bus_trajectories/io.py:load_avl_csv:
// it needs columns trip_id, route_id, latitude, longitude and an
// avl_event_time formatted "%Y-%m-%d %H:%M:%S.%f" (6-digit microseconds).
// Extra columns are carried along harmlessly (io.py reads dtype=str).

/** Epoch ms -> "YYYY-MM-DD HH:MM:SS.ffffff" in device-local time. */
export function fmtTime(ms) {
  if (ms == null) return "";
  const d = new Date(ms);
  const p = (n, w = 2) => String(n).padStart(w, "0");
  return (
    `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
    `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}.` +
    `${p(d.getMilliseconds(), 3)}000`
  );
}

function csvEscape(value) {
  if (value == null) return "";
  const s = String(value);
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

function toCsv(header, rows) {
  const lines = [header.join(",")];
  for (const row of rows) lines.push(row.map(csvEscape).join(","));
  return lines.join("\n") + "\n";
}

const num = (v, digits = 6) => (v == null ? "" : Number(v).toFixed(digits));

// ----------------------------------------------------------------- builders

export function buildPingsCsv(meta, pings) {
  return toCsv(
    ["trip_id", "bus_id", "route_id", "pattern_id", "avl_event_time",
     "latitude", "longitude", "accuracy_m", "heading", "speed_mps"],
    pings.map((p) => [
      meta.trip_id, meta.bus_id, meta.route_id, meta.pattern_id,
      fmtTime(p.t), num(p.lat), num(p.lon),
      num(p.accuracy, 1), num(p.heading, 1), num(p.speed, 2),
    ])
  );
}

export function buildEventsCsv(meta, events) {
  return toCsv(
    ["event_id", "trip_id", "type", "direction", "stop_id", "stop_name", "near_side",
     "note", "parent_id", "start_time", "end_time",
     "start_lat", "start_lon", "end_lat", "end_lon", "duration_s"],
    events.map((e) => [
      e.id, meta.trip_id, e.type, e.direction || "", e.stop_id || "", e.stop_name || "",
      e.near_side == null ? "" : (e.near_side ? "true" : "false"),
      e.note || "", e.parent_id || "",
      fmtTime(e.start_t), fmtTime(e.end_t),
      num(e.start_lat), num(e.start_lon), num(e.end_lat), num(e.end_lon),
      e.end_t != null ? ((e.end_t - e.start_t) / 1000).toFixed(1) : "",
    ])
  );
}

/**
 * Motion CSV from batched IndexedDB records (storage.getMotionBatches).
 * Also used by sync.js on a slice of batches to build an incremental chunk.
 */
export function buildMotionCsv(batches, { header = true } = {}) {
  const rows = [];
  for (const b of batches) {
    for (const s of b.samples) {
      rows.push([
        fmtTime(s.t), num(s.ax, 4), num(s.ay, 4), num(s.az, 4),
        num(s.gx, 4), num(s.gy, 4), num(s.gz, 4), num(s.interval_ms, 1),
      ]);
    }
  }
  const head = ["timestamp", "ax", "ay", "az", "gx", "gy", "gz", "interval_ms"];
  if (!header) return rows.map((r) => r.map(csvEscape).join(",")).join("\n") + (rows.length ? "\n" : "");
  return toCsv(head, rows);
}

export function buildMetaJson(meta, counts = {}) {
  return JSON.stringify(
    {
      ...meta,
      start_time: fmtTime(meta.start_t),
      end_time: fmtTime(meta.end_t),
      counts,
      device: navigator.userAgent,
      exported_at: fmtTime(Date.now()),
    },
    null,
    2
  );
}

// ----------------------------------------------------------------- download

export function downloadText(filename, text, mime = "text/csv") {
  const blob = new Blob([text], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}
