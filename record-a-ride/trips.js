// Desktop trips browser: lists everything in the R2 bucket via GET /api/trips
// (gated by the auto-save password), filters by date + observer, and downloads
// either a single file or one organized zip of every selected trip's CSVs.
// The append-only motion chunks are stitched into one motion.csv per trip.

const $ = (id) => document.getElementById(id);

let trips = [];                 // [{ key, files, meta }]
const selected = new Set();     // trip keys ticked for download

function token() { return $("token").value.trim(); }

function fileUrl(key, file, dl = true) {
  return `/api/trips/${encodeURIComponent(key)}/${file}` +
         `?token=${encodeURIComponent(token())}${dl ? "&dl=1" : ""}`;
}

function fmtBytes(n) {
  if (n > 1e6) return `${(n / 1e6).toFixed(1)} MB`;
  if (n > 1e3) return `${(n / 1e3).toFixed(0)} kB`;
  return `${n} B`;
}

const snapshotFiles = (t) => t.files.filter((f) => !f.name.startsWith("motion/"));
const motionChunks = (t) =>
  t.files.filter((f) => f.name.startsWith("motion/")).sort((a, b) => a.name.localeCompare(b.name));

// -------------------------------------------------------------------- load

async function loadTrips() {
  $("status").textContent = "Loading…";
  $("table").hidden = true;
  $("controls").hidden = true;
  localStorage.setItem("sync_token", token());
  let data;
  try {
    const resp = await fetch("/api/trips", { headers: { Authorization: `Bearer ${token()}` } });
    if (resp.status === 401) throw new Error("wrong password");
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    data = await resp.json();
  } catch (err) {
    $("status").textContent = `Failed to list trips: ${err.message || err}`;
    return;
  }

  if (!data.trips.length) { $("status").textContent = "No trips saved yet."; return; }

  // Pull each trip's meta.json up front — needed for the columns AND the filters.
  trips = data.trips;
  selected.clear();
  await Promise.all(trips.map(async (t) => {
    if (!snapshotFiles(t).some((f) => f.name === "meta.json")) { t.meta = null; return; }
    try {
      t.meta = await (await fetch(fileUrl(t.key, "meta.json", false))).json();
    } catch { t.meta = null; }
  }));

  $("controls").hidden = false;
  $("table").hidden = false;
  render();
}

// ------------------------------------------------------------------ filter

function filteredTrips() {
  const from = $("f-from").value;                 // "YYYY-MM-DD" or ""
  const to = $("f-to").value;
  const obs = $("f-observer").value.trim().toLowerCase();
  return trips.filter((t) => {
    const day = (t.meta?.start_time || "").slice(0, 10); // "YYYY-MM-DD"
    if (from && (!day || day < from)) return false;
    if (to && (!day || day > to)) return false;
    if (obs && !(t.meta?.observer || "").toLowerCase().includes(obs)) return false;
    return true;
  });
}

// ------------------------------------------------------------------ render

function render() {
  const list = filteredTrips();
  $("status").textContent =
    `${list.length} of ${trips.length} trip(s)` + (list.length !== trips.length ? " (filtered)" : "");

  const rows = $("rows");
  rows.innerHTML = "";
  for (const t of list) rows.appendChild(renderRow(t));
  updateDownloadButton();
}

function renderRow(t) {
  const tr = document.createElement("tr");

  const tdPick = document.createElement("td");
  tdPick.className = "pick";
  const cb = document.createElement("input");
  cb.type = "checkbox";
  cb.checked = selected.has(t.key);
  cb.onchange = () => { cb.checked ? selected.add(t.key) : selected.delete(t.key); updateDownloadButton(); };
  tdPick.appendChild(cb);

  const tdTrip = document.createElement("td");
  tdTrip.innerHTML = `<span class="badge">${t.key}</span>`;
  const tdRoute = document.createElement("td");
  const tdStart = document.createElement("td");
  const tdObs = document.createElement("td");
  if (t.meta) {
    tdRoute.textContent = `Rt ${t.meta.route_id || "?"} · #${t.meta.bus_id || "?"} → ${t.meta.destination || ""}`;
    tdStart.textContent = t.meta.start_time || "";
    tdObs.textContent = t.meta.observer || "";
  } else {
    tdRoute.innerHTML = `<span class="muted">no meta.json</span>`;
  }

  const tdFiles = document.createElement("td");
  tdFiles.className = "files";
  for (const f of snapshotFiles(t)) {
    const a = document.createElement("a");
    a.href = fileUrl(t.key, f.name);
    a.textContent = `${f.name} (${fmtBytes(f.size)})`;
    tdFiles.appendChild(a);
  }
  const chunks = motionChunks(t);
  if (chunks.length) {
    const total = chunks.reduce((acc, c) => acc + c.size, 0);
    const btn = document.createElement("button");
    btn.className = "rebuild";
    btn.textContent = `⬇ combined motion CSV — ${chunks.length} chunks, ${fmtBytes(total)}`;
    btn.onclick = () => downloadCombinedMotion(t.key, chunks, btn);
    tdFiles.appendChild(btn);
  }

  tr.append(tdPick, tdTrip, tdRoute, tdStart, tdObs, tdFiles);
  return tr;
}

function updateDownloadButton() {
  const n = selected.size;
  const btn = $("download-selected");
  btn.textContent = `⬇ Download selected (${n})`;
  btn.disabled = n === 0;
}

// ------------------------------------------------------------------ motion

async function fetchText(key, name) {
  const resp = await fetch(fileUrl(key, name, false));
  if (!resp.ok) throw new Error(`${name}: HTTP ${resp.status}`);
  return resp.text();
}

/** Stitch a trip's append-only motion chunks into one CSV string (or "" if none). */
async function stitchMotion(t) {
  const chunks = motionChunks(t);
  if (!chunks.length) return "";
  const parts = [];
  for (const c of chunks) parts.push(await fetchText(t.key, c.name));
  return parts.join("");
}

async function downloadCombinedMotion(key, chunks, btn) {
  const label = btn.textContent;
  const t = trips.find((x) => x.key === key);
  try {
    btn.textContent = "stitching…";
    const csv = await stitchMotion(t);
    saveBlob(new Blob([csv], { type: "text/csv" }), `motion_${key}.csv`);
  } catch (err) {
    btn.textContent = `failed: ${err.message || err}`;
    return;
  }
  btn.textContent = label;
}

// -------------------------------------------------------------- zip download

async function downloadSelected() {
  const chosen = trips.filter((t) => selected.has(t.key));
  if (!chosen.length) return;
  const btn = $("download-selected");
  const status = $("dl-status");
  btn.disabled = true;

  const zip = new JSZip();
  try {
    for (let i = 0; i < chosen.length; i++) {
      const t = chosen[i];
      status.textContent = `packing ${i + 1}/${chosen.length}: ${t.key}…`;
      const folder = zip.folder(t.key); // one folder per trip = organized
      // Small snapshot files verbatim.
      for (const f of snapshotFiles(t)) {
        folder.file(f.name, await fetchText(t.key, f.name));
      }
      // Motion chunks stitched into a single motion.csv.
      const motion = await stitchMotion(t);
      if (motion) folder.file("motion.csv", motion);
    }
    status.textContent = "building zip…";
    const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-");
    const blob = await zip.generateAsync({ type: "blob" });
    saveBlob(blob, `record-a-ride_trips_${stamp}.zip`);
    status.textContent = `✓ ${chosen.length} trip(s) downloaded`;
  } catch (err) {
    status.textContent = `Download failed: ${err.message || err}`;
  } finally {
    btn.disabled = selected.size === 0;
  }
}

function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 15000);
}

// -------------------------------------------------------------------- wire

$("token").value = localStorage.getItem("sync_token") || "";
$("load").onclick = loadTrips;
$("f-from").oninput = render;
$("f-to").oninput = render;
$("f-observer").oninput = render;
$("f-clear").onclick = () => { $("f-from").value = ""; $("f-to").value = ""; $("f-observer").value = ""; render(); };
$("select-all").onclick = () => { for (const t of filteredTrips()) selected.add(t.key); render(); };
$("select-none").onclick = () => { selected.clear(); render(); };
$("download-selected").onclick = downloadSelected;
if ($("token").value) loadTrips();
