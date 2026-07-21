// Network tab: filter panel + segment choropleth + detail panel.
//
// Reads S.network (filters, metric, minN). Data via NetworkData
// (../network_data.js); map via NetworkMap. Direction control follows the
// spec: enabled ONLY when exactly one route or a corridor is selected —
// whole-network views have no direction (a seg_id already encodes one
// direction of travel; "direction" only means something relative to a chosen
// route/corridor).

import * as d3 from "https://cdn.jsdelivr.net/npm/d3@7/+esm";
import { $ } from "../chart_util.js";
import { NetworkMap } from "../network_map.js";
import { deriveMetrics } from "../network_data.js";

export const METRICS = {
  mean_delay:      { label: "Mean delay (s)",        kind: "level", fmt: (v) => `${v.toFixed(0)}s` },
  median_delay:    { label: "Median delay (s)",      kind: "level", fmt: (v) => `${v.toFixed(0)}s` },
  p90_delay:       { label: "p90 delay (s)",         kind: "level", fmt: (v) => `${v.toFixed(0)}s` },
  buffer:          { label: "Buffer p90−p50 (s)",    kind: "level", fmt: (v) => `${v.toFixed(0)}s` },
  std_delay:       { label: "Delay std dev (s)",     kind: "level", fmt: (v) => `${v.toFixed(0)}s` },
  freeflow_speed:  { label: "Free-flow speed (mph)", kind: "static", fmt: (v) => `${v.toFixed(0)}` },
  peak_degradation:{ label: "Peak degradation (s)",  kind: "diff", fmt: (v) => `${v > 0 ? "+" : ""}${v.toFixed(0)}s` },
  buses_per_hr:    { label: "Buses / hour",          kind: "level", fmt: (v) => v.toFixed(1) },
};

const PERIOD_LABELS = {
  am_peak: "AM peak (6–10)", midday: "Midday (10–15)", pm_peak: "PM peak (15–19)",
  evening: "Evening (19–22)", late_night: "Late night (22–6)",
};

export class NetworkView {
  constructor(S) {
    this.S = S;
    this.map = null;
    this._panelBuilt = false;
    this._tooltip = null;
  }

  get data() { return this.S.network.data; }
  get F() { return this.S.network.filters; }

  // ---- lifecycle ---------------------------------------------------------

  async render() {
    const S = this.S;
    if (!this.data) return; // main.js loads NetworkData before first render
    if (!this._panelBuilt) this._buildPanel();
    if (!this.map) {
      this.map = new NetworkMap($("map"), this.data.segments, {
        onHover: (f, lngLat, point) => this._hover(f, point),
        onClick: (f) => this._select(f ? f.properties.sid : null),
      });
      this.S.network.map = this.map;
    }
    this._syncPanel();
    await this.refresh();
    setTimeout(() => this.map?.resize(), 60);
  }

  destroy() {
    this.map?.destroy();
    this.map = null;
    this.S.network.map = null;
    document.querySelector(".nw-panel")?.remove();
    document.querySelector(".nw-detail")?.remove();
    document.querySelector(".nw-tooltip")?.remove();
    this._panelBuilt = false;
  }

  // ---- filter panel ------------------------------------------------------

  _buildPanel() {
    const meta = this.data.meta;
    const dims = meta.dims;
    const el = document.createElement("div");
    el.className = "nw-panel";
    el.innerHTML = `
      <div class="nw-row"><b>Periods</b><span id="nw-periods">
        ${["am_peak", "midday", "pm_peak", "evening", "late_night"].map((p) =>
          `<label><input type="checkbox" data-p="${p}"> ${PERIOD_LABELS[p]}</label>`).join("")}
      </span></div>
      <div class="nw-row"><b>Days</b>
        <select id="nw-daytype">
          <option value="">all days</option>
          <option value="weekday" selected>weekday</option>
          <option value="sat">Saturday</option>
          <option value="sun">Sunday</option>
          ${["mon","tue","wed","thu","fri"].map((d, i) => `<option value="dow${i}">${d}</option>`).join("")}
        </select>
      </div>
      <div class="nw-row"><b>Pick</b><select id="nw-pick"><option value="">all</option>
        ${dims.picks.map((p, i) => `<option value="${i}">${p}</option>`).join("")}</select>
        <b>Season</b><select id="nw-season"><option value="">all</option>
        ${dims.seasons.map((s, i) => `<option value="${i}">${s}</option>`).join("")}</select>
        <b>Weather</b><select id="nw-weather"><option value="">all</option>
        ${dims.weathers.filter((w) => w !== "unknown").map((w) =>
          `<option value="${dims.weathers.indexOf(w)}">${w}</option>`).join("")}</select>
      </div>
      <div class="nw-row"><b>Route</b>
        <input id="nw-route-search" placeholder="filter…" size="6">
        <select id="nw-routes" multiple size="5"></select>
        <button id="nw-routes-clear" title="clear route selection">×</button>
      </div>
      <div class="nw-row"><b>Corridor</b>
        <select id="nw-corridor"><option value="">—</option>
          ${this.data.corridors.corridors.map((c) =>
            `<option value="${c.cid}">${c.name} (${c.routes.join(",")}) ${(c.len_m / 1609.344).toFixed(1)}mi</option>`).join("")}
        </select>
      </div>
      <div class="nw-row"><b>Direction</b>
        <select id="nw-direction" disabled><option value="">both</option></select>
        <span class="nw-note" id="nw-dir-note">pick one route or a corridor</span>
      </div>
      <div class="nw-row"><b>Min n</b>
        <input id="nw-minn" type="number" value="${this.S.network.minN}" min="1" max="10000" size="5">
        <span class="nw-note" id="nw-count"></span>
      </div>`;
    $("stage").appendChild(el);

    // Route list.
    const routeSel = el.querySelector("#nw-routes");
    const fillRoutes = (q = "") => {
      routeSel.innerHTML = "";
      dims.route_ids.forEach((r, i) => {
        if (q && !r.toLowerCase().includes(q.toLowerCase())) return;
        const o = document.createElement("option");
        o.value = i;
        o.textContent = r;
        routeSel.appendChild(o);
      });
    };
    fillRoutes();
    el.querySelector("#nw-route-search").oninput = (e) => fillRoutes(e.target.value);
    el.querySelector("#nw-routes-clear").onclick = () => {
      routeSel.selectedIndex = -1;
      this.F.routes = [];
      this._onFilterChange();
    };

    // Wiring.
    el.querySelectorAll("#nw-periods input").forEach((cb) => {
      cb.onchange = () => {
        this.F.periods = [...el.querySelectorAll("#nw-periods input:checked")].map((c) => c.dataset.p);
        this._onFilterChange();
      };
    });
    el.querySelector("#nw-daytype").onchange = (e) => {
      const v = e.target.value;
      if (v.startsWith("dow")) { this.F.daytype = null; this.F.dow = Number(v.slice(3)); }
      else { this.F.daytype = v || null; this.F.dow = null; }
      this._onFilterChange();
    };
    for (const [id, key] of [["nw-pick", "pick"], ["nw-season", "season"], ["nw-weather", "weather"]]) {
      el.querySelector(`#${id}`).onchange = (e) => {
        this.F[key] = e.target.value === "" ? null : Number(e.target.value);
        this._onFilterChange();
      };
    }
    routeSel.onchange = () => {
      this.F.routes = [...routeSel.selectedOptions].map((o) => Number(o.value));
      if (this.F.routes.length) { this.F.corridor = null; el.querySelector("#nw-corridor").value = ""; }
      this._onFilterChange();
    };
    el.querySelector("#nw-corridor").onchange = (e) => {
      this.F.corridor = e.target.value || null;
      if (this.F.corridor) { this.F.routes = []; routeSel.selectedIndex = -1; }
      this._onFilterChange();
    };
    el.querySelector("#nw-direction").onchange = (e) => {
      this.F.direction = e.target.value || null;
      this.refresh();
    };
    el.querySelector("#nw-minn").onchange = (e) => {
      this.S.network.minN = Math.max(1, Number(e.target.value) || 1);
      this.refresh();
    };
    this._panelBuilt = true;
  }

  _syncPanel() {
    const el = document.querySelector(".nw-panel");
    if (!el) return;
    el.querySelectorAll("#nw-periods input").forEach((cb) => {
      cb.checked = this.F.periods.includes(cb.dataset.p);
    });
  }

  // Direction options depend on the route/corridor selection (spec rule).
  _updateDirectionControl() {
    const el = document.querySelector("#nw-direction");
    const note = document.querySelector("#nw-dir-note");
    if (!el) return;
    const opts = ['<option value="">both</option>'];
    let enabled = false;
    if (this.F.corridor) {
      const c = this.data.corridors.corridors.find((x) => x.cid === this.F.corridor);
      if (c) {
        opts.push(`<option value="fwd">${c.dir_fwd}</option>`);
        if (c.dir_rev) opts.push(`<option value="rev">${c.dir_rev}</option>`);
        enabled = true;
      }
    } else if (this.F.routes.length === 1) {
      const rid = this.data.meta.dims.route_ids[this.F.routes[0]];
      const dirs = new Set();
      for (const f of this.data.segments.features) {
        for (const r of f.properties.routes) if (r.r === rid) dirs.add(r.dir);
      }
      for (const d of [...dirs].sort()) opts.push(`<option value="${d}">${d}</option>`);
      enabled = dirs.size > 0;
    }
    el.innerHTML = opts.join("");
    el.disabled = !enabled;
    note.style.display = enabled ? "none" : "";
    if (!enabled) this.F.direction = null;
  }

  _onFilterChange() {
    this._updateDirectionControl();
    this.refresh();
  }

  // ---- segment selection under route/corridor/direction ------------------

  _visibleSids() {
    const dims = this.data.meta.dims;
    if (this.F.corridor) {
      const c = this.data.corridors.corridors.find((x) => x.cid === this.F.corridor);
      if (!c) return null;
      if (this.F.direction === "fwd") return new Set(c.sids_fwd);
      if (this.F.direction === "rev") return new Set(c.sids_rev);
      return new Set([...c.sids_fwd, ...c.sids_rev]);
    }
    if (this.F.routes.length) {
      const wanted = new Set(this.F.routes.map((i) => dims.route_ids[i]));
      const out = new Set();
      for (const f of this.data.segments.features) {
        for (const r of f.properties.routes) {
          if (!wanted.has(r.r)) continue;
          if (this.F.direction && r.dir !== this.F.direction) continue;
          out.add(f.properties.sid);
        }
      }
      return out;
    }
    return null; // whole network
  }

  // ---- metric computation + painting -------------------------------------

  async refresh() {
    const metric = this.S.network.metric;
    const spec = METRICS[metric];
    const visible = this._visibleSids();
    const minN = this.S.network.minN;
    const values = new Map(); // sid -> number

    const tFf = new Map();
    const lenM = new Map();
    for (const f of this.data.segments.features) {
      tFf.set(f.properties.sid, f.properties.t_ff_s);
      lenM.set(f.properties.sid, f.properties.len_m);
    }

    if (spec.kind === "static") {
      // free-flow speed: no shards needed
      for (const f of this.data.segments.features) {
        const p = f.properties;
        if (p.t_ff_s > 0) values.set(p.sid, (p.len_m / p.t_ff_s) * 2.23694);
      }
    } else if (metric === "peak_degradation") {
      const base = { ...this.F, periods: ["midday"], daytype: this.F.daytype ?? "weekday" };
      const peak = { ...base, periods: ["am_peak", "pm_peak"] };
      const [a, b] = await Promise.all([this.data.combine(base), this.data.combine(peak)]);
      for (const [sid, accB] of b) {
        const accA = a.get(sid);
        if (!accA || accA.n < minN || accB.n < minN) continue;
        const mA = deriveMetrics(accA, tFf.get(sid), this.data.meta);
        const mB = deriveMetrics(accB, tFf.get(sid), this.data.meta);
        if (Number.isFinite(mA.median_delay_s) && Number.isFinite(mB.median_delay_s)) {
          values.set(sid, mB.median_delay_s - mA.median_delay_s);
        }
      }
    } else {
      const combined = await this.data.combine(this.F);
      const nDates = this.data.dateCount(this.F);
      const hours = this.data.periodHours(this.F);
      for (const [sid, acc] of combined) {
        if (acc.n < minN) continue;
        if (metric === "buses_per_hr") {
          if (nDates > 0 && hours > 0) values.set(sid, acc.n / (nDates * hours));
          continue;
        }
        const m = deriveMetrics(acc, tFf.get(sid), this.data.meta);
        const v = {
          mean_delay: m.mean_delay_s, median_delay: m.median_delay_s,
          p90_delay: m.p90_delay_s, buffer: m.buffer_s, std_delay: m.std_delay_s,
        }[metric];
        if (Number.isFinite(v)) values.set(sid, v);
      }
    }

    this._lastValues = values;
    this._paint(values, spec, visible);
    const nShown = visible
      ? [...values.keys()].filter((s) => visible.has(s)).length
      : values.size;
    const cnt = document.querySelector("#nw-count");
    if (cnt) cnt.textContent = `${nShown} segments`;
  }

  _paint(values, spec, visible) {
    const vals = [...values.entries()]
      .filter(([sid]) => !visible || visible.has(sid))
      .map(([, v]) => v);
    if (!vals.length) {
      this.map.setColors(new Map());
      this.map.setLegend({ title: spec.label, gradient: ["#ddd", "#ddd"], ticks: ["no data", ""], note: "" });
      return;
    }
    const lo = d3.quantile(vals.slice().sort(d3.ascending), 0.02);
    const hi = d3.quantile(vals.slice().sort(d3.ascending), 0.98);

    let colorOf, gradient;
    if (spec.kind === "diff") {
      const m = Math.max(Math.abs(lo), Math.abs(hi)) || 1;
      const sc = d3.scaleSequential(d3.interpolateRdBu).domain([m, -m]); // red = worse
      colorOf = (v) => sc(v);
      gradient = d3.range(0, 1.01, 0.1).map((t) => sc.interpolator()(1 - t));
    } else if (spec.kind === "static") {
      const sc = d3.scaleSequential(d3.interpolateViridis).domain([lo, hi]);
      colorOf = (v) => sc(v);
      gradient = d3.range(0, 1.01, 0.1).map((t) => d3.interpolateViridis(t));
    } else {
      const sc = d3.scaleSequential(d3.interpolateYlOrRd).domain([lo, hi]);
      colorOf = (v) => sc(v);
      gradient = d3.range(0, 1.01, 0.1).map((t) => d3.interpolateYlOrRd(t));
    }

    const colors = new Map();
    for (const [sid, v] of values) {
      if (visible && !visible.has(sid)) continue;
      colors.set(sid, { color: colorOf(Math.max(lo, Math.min(hi, v))) });
    }
    this.map.setColors(colors);
    this.map.setLegend({
      title: spec.label,
      gradient,
      ticks: [spec.fmt(lo), spec.fmt((lo + hi) / 2), spec.fmt(hi)],
      note: `p2–p98 across shown segments · grey = n < ${this.S.network.minN}`,
    });
  }

  // ---- hover tooltip + detail panel --------------------------------------

  _hover(f, point) {
    if (!this._tooltip) {
      this._tooltip = document.createElement("div");
      this._tooltip.className = "nw-tooltip hidden";
      document.body.appendChild(this._tooltip);
    }
    if (!f) { this._tooltip.classList.add("hidden"); return; }
    const p = f.properties;
    const routes = JSON.parse(p.routes || "[]");
    const v = this._lastValues?.get(p.sid);
    const spec = METRICS[this.S.network.metric];
    this._tooltip.innerHTML = `
      <b>${p.label}</b><br>
      routes: ${routes.map((r) => `${r.r} ${r.dir}`).join(", ")}<br>
      ${spec.label}: ${v == null ? "—" : spec.fmt(v)} · ${Math.round(p.len_m)} m`;
    const mapRect = $("map").getBoundingClientRect();
    this._tooltip.style.left = `${mapRect.left + point.x + 12}px`;
    this._tooltip.style.top = `${mapRect.top + point.y + 12}px`;
    this._tooltip.classList.remove("hidden");
  }

  async _select(sid) {
    document.querySelector(".nw-detail")?.remove();
    this.S.network.selected = sid;
    if (sid == null) return;
    const f = this.data.segments.features.find((x) => x.properties.sid === sid);
    if (!f) return;
    const p = f.properties;
    this.map.highlight([sid], { zoom: false });

    // Per-period medians/p90s under the current non-period filters.
    const periods = ["am_peak", "midday", "pm_peak", "evening", "late_night"];
    const perPeriod = [];
    for (const period of periods) {
      const acc = (await this.data.combine({ ...this.F, periods: [period] })).get(sid);
      perPeriod.push(acc ? deriveMetrics(acc, p.t_ff_s, this.data.meta) : null);
    }

    const el = document.createElement("div");
    el.className = "nw-detail";
    const ffMph = p.t_ff_s > 0 ? ((p.len_m / p.t_ff_s) * 2.23694).toFixed(0) : "—";
    el.innerHTML = `
      <button class="nw-close">×</button>
      <h3>${p.label}</h3>
      <div class="nw-chips">${p.routes.map((r) => `<span class="chip">${r.r} ${r.dir}</span>`).join("")}</div>
      <div class="nw-facts">
        ${Math.round(p.len_m)} m · ${p.n_stops} stop${p.n_stops === 1 ? "" : "s"} ·
        free-flow ${ffMph} mph (${p.ff_method || "—"})
        ${p.rev_sid != null ? ` · <a href="#" id="nw-rev">reverse direction →</a>` : ""}
      </div>
      <table class="nw-ptable"><tr><th></th><th>n</th><th>median</th><th>p90</th><th>buffer</th></tr>
      ${periods.map((per, i) => {
        const m = perPeriod[i];
        return `<tr><td>${PERIOD_LABELS[per].split(" (")[0]}</td>
          ${m && m.n ? `<td>${m.n}</td><td>${m.median_delay_s.toFixed(0)}s</td>
           <td>${m.p90_delay_s.toFixed(0)}s</td><td>${m.buffer_s.toFixed(0)}s</td>`
          : "<td>—</td><td>—</td><td>—</td><td>—</td>"}</tr>`;
      }).join("")}
      </table>`;
    $("stage").appendChild(el);
    el.querySelector(".nw-close").onclick = () => { el.remove(); this.map.highlight([]); };
    el.querySelector("#nw-rev")?.addEventListener("click", (e) => {
      e.preventDefault();
      this._select(p.rev_sid);
    });
  }
}
