// Desktop trips browser: lists everything in the R2 bucket via
// GET /api/trips, shows per-file download links (token passed as a query
// param so plain <a href> works), and can stitch the append-only motion
// chunks back into a single CSV client-side.

const $ = (id) => document.getElementById(id);

function token() {
  return $("token").value.trim();
}

function fileUrl(key, file, dl = true) {
  return `/api/trips/${encodeURIComponent(key)}/${file}` +
         `?token=${encodeURIComponent(token())}${dl ? "&dl=1" : ""}`;
}

function fmtBytes(n) {
  if (n > 1e6) return `${(n / 1e6).toFixed(1)} MB`;
  if (n > 1e3) return `${(n / 1e3).toFixed(0)} kB`;
  return `${n} B`;
}

async function loadTrips() {
  $("status").textContent = "Loading…";
  $("table").hidden = true;
  localStorage.setItem("sync_token", token());
  let data;
  try {
    const resp = await fetch("/api/trips", {
      headers: { Authorization: `Bearer ${token()}` },
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    data = await resp.json();
  } catch (err) {
    $("status").textContent = `Failed to list trips: ${err.message || err}`;
    return;
  }

  const rows = $("rows");
  rows.innerHTML = "";
  if (!data.trips.length) {
    $("status").textContent = "No trips saved yet.";
    return;
  }
  $("status").textContent = `${data.trips.length} trip(s)`;
  $("table").hidden = false;

  for (const trip of data.trips) {
    const tr = document.createElement("tr");
    const tdTrip = document.createElement("td");
    const tdRoute = document.createElement("td");
    const tdStart = document.createElement("td");
    const tdObs = document.createElement("td");
    const tdFiles = document.createElement("td");
    tdFiles.className = "files";

    tdTrip.innerHTML = `<span class="badge">${trip.key}</span>`;

    const snapshots = trip.files.filter((f) => !f.name.startsWith("motion/"));
    const chunks = trip.files
      .filter((f) => f.name.startsWith("motion/"))
      .sort((a, b) => a.name.localeCompare(b.name));

    for (const f of snapshots) {
      const a = document.createElement("a");
      a.href = fileUrl(trip.key, f.name);
      a.textContent = `${f.name} (${fmtBytes(f.size)})`;
      tdFiles.appendChild(a);
    }
    if (chunks.length) {
      const total = chunks.reduce((acc, c) => acc + c.size, 0);
      const btn = document.createElement("button");
      btn.className = "rebuild";
      btn.textContent = `⬇ combined motion CSV — ${chunks.length} chunks, ${fmtBytes(total)}`;
      btn.onclick = () => downloadCombinedMotion(trip.key, chunks, btn);
      tdFiles.appendChild(btn);
    }

    // Pull meta.json for the human-readable columns.
    if (snapshots.some((f) => f.name === "meta.json")) {
      fetch(fileUrl(trip.key, "meta.json", false))
        .then((r) => r.json())
        .then((meta) => {
          tdRoute.textContent = `Rt ${meta.route_id} · #${meta.bus_id} → ${meta.destination || ""}`;
          tdStart.textContent = meta.start_time || "";
          tdObs.textContent = meta.observer || "";
        })
        .catch(() => {});
    }

    tr.append(tdTrip, tdRoute, tdStart, tdObs, tdFiles);
    rows.appendChild(tr);
  }
}

async function downloadCombinedMotion(key, chunks, btn) {
  const label = btn.textContent;
  const parts = [];
  for (let i = 0; i < chunks.length; i++) {
    btn.textContent = `fetching chunk ${i + 1}/${chunks.length}…`;
    const resp = await fetch(fileUrl(key, chunks[i].name, false));
    if (!resp.ok) {
      btn.textContent = `chunk ${chunks[i].name} failed (HTTP ${resp.status})`;
      return;
    }
    parts.push(await resp.text());
  }
  btn.textContent = label;
  const blob = new Blob(parts, { type: "text/csv" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `motion_${key}.csv`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 10000);
}

$("token").value = localStorage.getItem("sync_token") || "";
$("load").onclick = loadTrips;
if ($("token").value) loadTrips();
