// Areas-of-interest sub-tab: ranked outlier list with map click-through.
//
// Reads data/network/areas.json (produced offline by
// analysis/network/areas_of_interest.py). Row click → highlight + zoom the
// network map to the entity's segments.

import { $ } from "../chart_util.js";
import { cleanLabel } from "../network_data.js";

const METRIC_LABELS = {
  mean_delay_ratio: "mean delay ÷ free-flow",
  median_delay_ratio: "median delay ÷ free-flow",
  buffer_ratio: "buffer (p90−p50 ratio)",
  cv_delay: "delay variability (CV)",
};

const KIND_LABELS = { segment: "Segments", corridor: "Corridors", route: "Routes" };

const fmtV = (v) => (Math.abs(v) >= 10 ? v.toFixed(1) : v.toFixed(2));

export class AreasView {
  constructor(S) {
    this.S = S;
    this.areas = null;
    this._loaded = false;
    this.weighted = true;
    this.context = null;
  }

  get data() { return this.S.network.data; }

  async render() {
    const host = $("chart");
    if (!this._loaded) {
      try {
        this.areas = await fetch("../data/network/areas.json", { cache: "no-store" })
          .then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json(); });
      } catch (e) {
        host.innerHTML = `<div class="nw-empty">No areas.json yet — run
          <code>analysis/network/areas_of_interest.py</code> and copy the output to
          <code>dashboard/data/network/areas.json</code>. (${e.message})</div>`;
        return;
      }
      this._loaded = true;
      this.context = this.areas.contexts[0]?.id ?? null;
    }
    if (!this.areas.contexts.length) {
      host.innerHTML = `<div class="nw-empty">areas.json has no contexts.</div>`;
      return;
    }

    const groups = { level: [], diff: [] };
    for (const c of this.areas.contexts) groups[c.type].push(c);
    const ctx = this.areas.contexts.find((c) => c.id === this.context) ?? this.areas.contexts[0];
    this.context = ctx.id;

    const filterLabel = (f) =>
      [f.daytype?.replace(":", " · "), f.period ?? "all periods"].filter(Boolean).join(" · ");
    const opt = (c) => {
      const f = c.type === "level"
        ? filterLabel(c.filter)
        : `${filterLabel(c.filterA)} → ${filterLabel(c.filterB)}`;
      return `<option value="${c.id}" ${c.id === ctx.id ? "selected" : ""}>
        ${KIND_LABELS[c.kind]} · ${f} · ${METRIC_LABELS[c.metric] ?? c.metric}</option>`;
    };

    host.innerHTML = `
      <div class="nw-areas">
        <div class="nw-areas-bar">
          <div class="row1">
            <select id="aoi-ctx">
              <optgroup label="Outliers within a filter">${groups.level.map(opt).join("")}</optgroup>
              <optgroup label="Biggest changes between filters">${groups.diff.map(opt).join("")}</optgroup>
            </select>
          </div>
          <div class="row2">
            <label><input type="checkbox" id="aoi-weighted" ${this.weighted ? "checked" : ""}>
              weight by service intensity</label>
            <span>${ctx.n_entities} ${ctx.kind}s ranked</span>
            <span>network median ${fmtV(ctx.network.median)} ± ${fmtV(ctx.network.mad)} MAD</span>
            <span style="color:#999">click a row to zoom the map</span>
          </div>
        </div>
        <div class="nw-areas-scroll">
        <table class="nw-atable">
          <thead><tr>
            <th class="nw-rank">#</th><th class="nw-aname">name</th>
            ${ctx.type === "level"
              ? "<th>value</th>"
              : "<th>before</th><th>after</th><th>Δ</th>"}
            <th title="robust z-score, shrunk toward 0 for small samples">z*</th>
            <th>n</th><th>bus/hr</th>
          </tr></thead>
          <tbody></tbody>
        </table>
        </div>
      </div>`;

    const entities = [...ctx.entities].sort((a, b) =>
      this.weighted
        ? Math.abs(b.priority) - Math.abs(a.priority)
        : Math.abs(b.z_shrunk) - Math.abs(a.z_shrunk));

    const tbody = host.querySelector("tbody");
    entities.forEach((e, i) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td class="nw-rank">${i + 1}</td>
        <td class="nw-aname">
          <span class="kbadge kbadge-${ctx.kind}" title="${KIND_LABELS[ctx.kind]}">${ctx.kind[0].toUpperCase()}</span>
          ${cleanLabel(e.name)}
        </td>
        ${ctx.type === "level"
          ? `<td>${fmtV(e.value)}</td>`
          : `<td>${fmtV(e.valueA)}</td><td>${fmtV(e.valueB)}</td>
             <td class="${e.delta > 0 ? "worse" : "better"}">${e.delta > 0 ? "+" : ""}${fmtV(e.delta)}</td>`}
        <td>${e.z_shrunk.toFixed(1)}</td>
        <td>${ctx.type === "level" ? e.n : `${e.nA}/${e.nB}`}</td>
        <td>${e.buses_per_hr.toFixed(1)}</td>`;
      tr.onclick = () => this._showOnMap(ctx, e, tr);
      tbody.appendChild(tr);
    });

    host.querySelector("#aoi-ctx").onchange = (e) => { this.context = e.target.value; this.render(); };
    host.querySelector("#aoi-weighted").onchange = (e) => { this.weighted = e.target.checked; this.render(); };
  }

  _showOnMap(ctx, e, tr) {
    document.querySelectorAll(".nw-atable tr.sel").forEach((r) => r.classList.remove("sel"));
    tr.classList.add("sel");
    const map = this.S.network.map;
    if (!map) return;
    const bySegId = new Map(
      this.data.segments.features.map((f) => [f.properties.seg_id, f.properties.sid]),
    );
    let sids;
    if (ctx.kind === "route") {
      sids = this.data.segments.features
        .filter((f) => f.properties.routes.some((r) => r.r === e.eid))
        .map((f) => f.properties.sid);
    } else {
      sids = e.seg_ids.map((s) => bySegId.get(s)).filter((s) => s != null);
    }
    map.highlight(sids, { zoom: true });
  }
}
