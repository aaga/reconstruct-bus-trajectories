// Network tab: filter panel + segment choropleth + detail panel.
//
// Reads S.network (filters, metric, minN). Data via NetworkData
// (../network_data.js); map via NetworkMap. Direction control follows the
// spec: enabled ONLY when exactly one route or >=1 corridor is selected —
// whole-network views have no direction filter (a seg_id already encodes one
// direction of travel; "direction" only means something relative to a chosen
// route/corridor).
//
// Corridor picking from the map: hold "c" and hover to preview the corridor
// under the cursor; click to select it as the filter; shift-click to
// add/remove from a multi-selection.

import * as d3 from "https://cdn.jsdelivr.net/npm/d3@7/+esm";
import { $ } from "../chart_util.js";
import { NetworkMap } from "../network_map.js";
import { deriveMetrics, cleanLabel } from "../network_data.js";

export const METRICS = {
  mean_delay:      { label: "Mean delay (s)",        kind: "level", fmt: (v) => `${v.toFixed(0)}s` },
  median_delay:    { label: "Median delay (s)",      kind: "level", fmt: (v) => `${v.toFixed(0)}s` },
  p90_delay:       { label: "p90 delay (s)",         kind: "level", fmt: (v) => `${v.toFixed(0)}s` },
  buffer:          { label: "Buffer p90−p50 (s)",    kind: "level", fmt: (v) => `${v.toFixed(0)}s` },
  std_delay:       { label: "Delay std dev (s)",     kind: "level", fmt: (v) => `${v.toFixed(0)}s` },
  freeflow_speed:  { label: "Free-flow speed (mph)", kind: "static", fmt: (v) => `${v.toFixed(0)}` },
  peak_degradation:{ label: "Peak degradation (s)",  kind: "diff", fmt: (v) => `${v > 0 ? "+" : ""}${v.toFixed(0)}s` },
  buses_per_hr:    { label: "Buses / hour",          kind: "level", fmt: (v) => v.toFixed(1) },
  // Door/APC-derived (dates with bus-state-history coverage only):
  dwell_delay:     { label: "At-stop dwell (s)",     kind: "door", fmt: (v) => `${v.toFixed(0)}s` },
  moving_delay:    { label: "In-motion delay (s)",   kind: "door", fmt: (v) => `${v.toFixed(0)}s` },
  dwell_share:     { label: "Dwell share of delay",  kind: "door", fmt: (v) => `${(100 * v).toFixed(0)}%` },
  boardings_per_hr:{ label: "Boardings / hour",      kind: "door", fmt: (v) => v.toFixed(1) },
};

const PERIOD_LABELS = {
  am_peak: "AM peak", midday: "Midday", pm_peak: "PM peak",
  evening: "Evening", late_night: "Late night",
};
const PERIOD_HOURS = {
  am_peak: "6:00–10:00", midday: "10:00–15:00", pm_peak: "15:00–19:00",
  evening: "19:00–22:00", late_night: "22:00–6:00",
};

export class NetworkView {
  constructor(S) {
    this.S = S;
    this.map = null;
    this._panelBuilt = false;
    this._tooltip = null;
    this._corMode = false;      // "c" held: corridor hover/click mode
    this._corPreview = null;    // cid currently previewed on hover
    this._keyHandlersBound = false;
  }

  get data() { return this.S.network.data; }
  get F() { return this.S.network.filters; }

  // ---- lifecycle ---------------------------------------------------------

  async render() {
    if (!this.data) return; // main.js loads NetworkData before first render
    if (!this._panelBuilt) this._buildPanel();
    if (!this.map) {
      this.map = new NetworkMap($("map"), this.data.segments, {
        onHover: (f, lngLat, point) => this._hover(f, point),
        onClick: (f, ev) => this._click(f, ev),
      });
      this.S.network.map = this.map;
    }
    this._bindKeys();
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
    this._tooltip?.remove();
    this._tooltip = null;
    this._panelBuilt = false;
  }

  _bindKeys() {
    if (this._keyHandlersBound) return;
    this._keyHandlersBound = true;
    const typing = (e) => ["INPUT", "SELECT", "TEXTAREA"].includes(e.target.tagName);
    document.addEventListener("keydown", (e) => {
      if (this.S.main !== "network" || typing(e)) return;
      if (e.key.toLowerCase() === "c" && !e.metaKey && !e.ctrlKey) this._corMode = true;
    });
    document.addEventListener("keyup", (e) => {
      if (e.key.toLowerCase() === "c") {
        this._corMode = false;
        this._corPreview = null;
        if (this.map && this.S.main === "network") this._restoreHighlight();
      }
    });
    window.addEventListener("blur", () => { this._corMode = false; });
  }

  // ---- filter panel ------------------------------------------------------

  _buildPanel() {
    const meta = this.data.meta;
    const dims = meta.dims;
    const el = document.createElement("div");
    el.className = "nw-panel";
    el.innerHTML = `
      <div class="nw-group"><b>Periods</b>
        <span class="nw-checks" id="nw-periods">
        ${["am_peak", "midday", "pm_peak", "evening", "late_night"].map((p) =>
          `<label title="${PERIOD_HOURS[p]}"><input type="checkbox" data-p="${p}"> ${PERIOD_LABELS[p]}</label>`).join("")}
        </span>
      </div>
      <div class="nw-group"><b>Days</b>
        <select id="nw-daytype">
          <option value="">all days</option>
          <option value="weekday" selected>weekday (excl. holidays)</option>
          <option value="sat">Saturday</option>
          <option value="sun">Sunday</option>
          ${["mon","tue","wed","thu","fri"].map((d, i) => `<option value="dow${i}">${d} only</option>`).join("")}
        </select>
      </div>
      <div class="nw-group"><b>Pick / Season / Weather</b>
        <div class="nw-inline">
          <select id="nw-pick"><option value="">pick: all</option>
            ${dims.picks.map((p, i) => `<option value="${i}">${p}</option>`).join("")}</select>
          <select id="nw-season"><option value="">season: all</option>
            ${dims.seasons.map((s, i) => `<option value="${i}">${s}</option>`).join("")}</select>
        </div>
        <div class="nw-inline" style="margin-top:4px">
          <select id="nw-weather"><option value="">weather: all</option>
            ${dims.weathers.filter((w) => w !== "unknown").map((w) =>
              `<option value="${dims.weathers.indexOf(w)}">${w}</option>`).join("")}</select>
        </div>
      </div>
      <div class="nw-group"><b>Routes</b>
        <div class="nw-minirow" style="margin-bottom:4px">
          <input id="nw-route-search" placeholder="type to filter…">
          <button class="nw-clear" id="nw-routes-clear" title="clear route selection">×</button>
        </div>
        <select id="nw-routes" multiple size="5"></select>
      </div>
      <div class="nw-group"><b>Corridors</b>
        <div class="nw-minirow" style="margin-bottom:4px">
          <span class="nw-note">hold <span class="nw-hintkey">c</span> + click map · shift = multi</span>
          <button class="nw-clear" id="nw-corridors-clear" title="clear corridor selection">×</button>
        </div>
        <select id="nw-corridors" multiple size="5">
          ${this.data.corridors.corridors.map((c) =>
            `<option value="${c.cid}">${c.name} · ${c.routes.slice(0, 3).join(",")}${c.routes.length > 3 ? "…" : ""} · ${(c.len_m / 1609.344).toFixed(1)}mi</option>`).join("")}
        </select>
      </div>
      <div class="nw-group"><b>Direction</b>
        <select id="nw-direction" disabled><option value="">both</option></select>
        <span class="nw-note" id="nw-dir-note">select one route or corridor(s)</span>
      </div>
      <div class="nw-group"><b>Min traversals</b>
        <div class="nw-minirow">
          <input id="nw-minn" type="number" value="${this.S.network.minN}" min="1" max="10000">
          <span class="nw-note" id="nw-count"></span>
        </div>
      </div>`;
    $("stage").appendChild(el);

    // Route list.
    const routeSel = el.querySelector("#nw-routes");
    const fillRoutes = (q = "") => {
      const selected = new Set(this.F.routes);
      routeSel.innerHTML = "";
      dims.route_ids.forEach((r, i) => {
        if (q && !r.toLowerCase().includes(q.toLowerCase())) return;
        const o = document.createElement("option");
        o.value = i;
        o.textContent = r;
        o.selected = selected.has(i);
        routeSel.appendChild(o);
      });
    };
    fillRoutes();
    el.querySelector("#nw-route-search").oninput = (e) => fillRoutes(e.target.value);
    el.querySelector("#nw-routes-clear").onclick = () => {
      this.F.routes = [];
      fillRoutes(el.querySelector("#nw-route-search").value);
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
      if (this.F.routes.length) this._setCorridors([]);
      this._onFilterChange();
    };
    el.querySelector("#nw-corridors").onchange = (e) => {
      this._setCorridors([...e.target.selectedOptions].map((o) => o.value), { syncPanel: false });
      if (this.F.corridors.length) { this.F.routes = []; fillRoutes(el.querySelector("#nw-route-search").value); }
      this._onFilterChange();
    };
    el.querySelector("#nw-corridors-clear").onclick = () => {
      this._setCorridors([]);
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
    this._updateDirectionControl();
  }

  _setCorridors(cids, { syncPanel = true } = {}) {
    this.F.corridors = cids;
    if (syncPanel) {
      const sel = document.querySelector("#nw-corridors");
      if (sel) for (const o of sel.options) o.selected = cids.includes(o.value);
    }
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
    const cors = this.F.corridors
      .map((cid) => this.data.corridors.corridors.find((x) => x.cid === cid))
      .filter(Boolean);
    if (cors.length === 1) {
      opts.push(`<option value="fwd">${cors[0].dir_fwd}</option>`);
      if (cors[0].dir_rev) opts.push(`<option value="rev">${cors[0].dir_rev}</option>`);
      enabled = true;
    } else if (cors.length > 1) {
      opts.push('<option value="fwd">forward</option>', '<option value="rev">reverse</option>');
      enabled = true;
    } else if (this.F.routes.length === 1) {
      const rid = this.data.meta.dims.route_ids[this.F.routes[0]];
      const dirs = new Set();
      for (const f of this.data.segments.features) {
        for (const r of f.properties.routes) if (r.r === rid) dirs.add(r.dir);
      }
      for (const d of [...dirs].sort()) opts.push(`<option value="${d}">${d}</option>`);
      enabled = dirs.size > 0;
    }
    const prev = this.F.direction;
    el.innerHTML = opts.join("");
    el.disabled = !enabled;
    note.style.display = enabled ? "none" : "";
    if (!enabled) this.F.direction = null;
    else if (prev && [...el.options].some((o) => o.value === prev)) el.value = prev;
    else this.F.direction = null;
  }

  _onFilterChange() {
    this._updateDirectionControl();
    this.refresh();
  }

  // ---- segment selection under route/corridor/direction ------------------

  _visibleSids() {
    const dims = this.data.meta.dims;
    if (this.F.corridors.length) {
      const out = new Set();
      for (const cid of this.F.corridors) {
        const c = this.data.corridors.corridors.find((x) => x.cid === cid);
        if (!c) continue;
        if (this.F.direction !== "rev") for (const s of c.sids_fwd) out.add(s);
        if (this.F.direction !== "fwd") for (const s of c.sids_rev) out.add(s);
      }
      return out;
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
    for (const f of this.data.segments.features) {
      tFf.set(f.properties.sid, f.properties.t_ff_s);
    }

    if (spec.kind === "static") {
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
    } else if (spec.kind === "door") {
      const combined = await this.data.combine(this.F);
      const nDoorDates = this.data.doorDateCount(this.F);
      const hours = this.data.periodHours(this.F);
      for (const [sid, acc] of combined) {
        if ((acc.nDoor ?? 0) < minN) continue; // min n applies to the covered subset
        if (metric === "boardings_per_hr") {
          if (nDoorDates > 0 && hours > 0) values.set(sid, acc.sumOns / (nDoorDates * hours));
          continue;
        }
        const m = deriveMetrics(acc, tFf.get(sid), this.data.meta);
        const v = {
          dwell_delay: m.mean_dwell_s,
          moving_delay: m.moving_delay_s,
          dwell_share: m.dwell_share,
        }[metric];
        if (Number.isFinite(v)) values.set(sid, v);
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
    const sorted = vals.slice().sort(d3.ascending);
    const lo = d3.quantile(sorted, 0.02);
    const hi = d3.quantile(sorted, 0.98);

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
    const covNote = spec.kind === "door"
      ? ` · door data: ${this.data.meta.n_door_dates ?? "?"} of ${this.data.meta.n_dates} days`
      : "";
    this.map.setLegend({
      title: spec.label,
      gradient,
      ticks: [spec.fmt(lo), spec.fmt((lo + hi) / 2), spec.fmt(hi)],
      note: `p2–p98 across shown segments · grey = n < ${this.S.network.minN}${covNote}`,
    });
  }

  // ---- corridor resolution for map interactions --------------------------

  _corridorOf(feature) {
    // A segment can belong to several corridors; prefer the longest.
    let cids = feature.properties.corridors;
    if (typeof cids === "string") cids = JSON.parse(cids || "[]");
    if (!cids?.length) return null;
    const cors = cids
      .map((cid) => this.data.corridors.corridors.find((c) => c.cid === cid))
      .filter(Boolean);
    cors.sort((a, b) => b.len_m - a.len_m);
    return cors[0] ?? null;
  }

  _corridorSids(cid) {
    const c = this.data.corridors.corridors.find((x) => x.cid === cid);
    return c ? [...c.sids_fwd, ...c.sids_rev] : [];
  }

  _restoreHighlight() {
    // After corridor preview ends, restore selection highlight (if any).
    const sids = this.F.corridors.flatMap((cid) => this._corridorSids(cid));
    this.map.highlight(sids, { zoom: false });
  }

  // ---- hover tooltip + clicks --------------------------------------------

  _hover(f, point) {
    if (!this._tooltip) {
      this._tooltip = document.createElement("div");
      this._tooltip.className = "nw-tooltip hidden";
      document.body.appendChild(this._tooltip);
    }
    if (!f) {
      this._tooltip.classList.add("hidden");
      if (this._corMode && this._corPreview) {
        this._corPreview = null;
        this._restoreHighlight();
      }
      return;
    }
    const p = f.properties;
    const routes = typeof p.routes === "string" ? JSON.parse(p.routes) : p.routes;

    if (this._corMode) {
      const cor = this._corridorOf(f);
      if (cor) {
        if (this._corPreview !== cor.cid) {
          this._corPreview = cor.cid;
          this.map.highlight(this._corridorSids(cor.cid), { zoom: false });
        }
        this._tooltip.innerHTML =
          `<span class="tt-cor"><b>${cor.name}</b> corridor</span><br>` +
          `routes ${cor.routes.join(", ")} · ${(cor.len_m / 1609.344).toFixed(1)} mi<br>` +
          `click to filter · shift-click to add`;
      } else {
        this._tooltip.innerHTML = `<i>not part of a corridor</i>`;
      }
    } else {
      const v = this._lastValues?.get(p.sid);
      const spec = METRICS[this.S.network.metric];
      this._tooltip.innerHTML = `
        <b>${cleanLabel(p.label)}</b><br>
        routes: ${routes.map((r) => `${r.r} ${r.dir}`).join(", ")}<br>
        ${spec.label}: ${v == null ? "—" : spec.fmt(v)} · ${Math.round(p.len_m)} m`;
    }
    const mapRect = $("map").getBoundingClientRect();
    this._tooltip.style.left = `${mapRect.left + point.x + 12}px`;
    this._tooltip.style.top = `${mapRect.top + point.y + 12}px`;
    this._tooltip.classList.remove("hidden");
  }

  _click(f, ev) {
    if (this._corMode) {
      if (!f) return;
      const cor = this._corridorOf(f);
      if (!cor) return;
      const cur = this.F.corridors;
      let next;
      if (ev?.shiftKey) {
        next = cur.includes(cor.cid) ? cur.filter((c) => c !== cor.cid) : [...cur, cor.cid];
      } else {
        next = [cor.cid];
      }
      this._setCorridors(next);
      if (next.length) this.F.routes = [];
      this._onFilterChange();
      this._restoreHighlight();
      return;
    }
    this._select(f ? f.properties.sid : null);
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
      <h3>${cleanLabel(p.label)}</h3>
      <div class="nw-chips">${p.routes.map((r) => `<span class="chip">${r.r} ${r.dir}</span>`).join("")}</div>
      <div class="nw-facts">
        ${Math.round(p.len_m)} m · ${p.n_stops} stop${p.n_stops === 1 ? "" : "s"} ·
        free-flow ${ffMph} mph
        ${p.rev_sid != null ? ` · <a href="#" id="nw-rev">reverse direction →</a>` : ""}
      </div>
      <table class="nw-ptable"><tr><th></th><th>n</th><th>median</th><th>p90</th><th>buffer</th><th>dwell</th><th>moving</th></tr>
      ${periods.map((per, i) => {
        const m = perPeriod[i];
        const door = m && m.n_door > 0 && Number.isFinite(m.mean_dwell_s)
          ? `<td>${m.mean_dwell_s.toFixed(0)}s</td><td>${m.moving_delay_s.toFixed(0)}s</td>`
          : "<td>—</td><td>—</td>";
        return `<tr><td>${PERIOD_LABELS[per].split(" (")[0]}</td>
          ${m && m.n ? `<td>${m.n}</td><td>${m.median_delay_s.toFixed(0)}s</td>
           <td>${m.p90_delay_s.toFixed(0)}s</td><td>${m.buffer_s.toFixed(0)}s</td>${door}`
          : "<td>—</td><td>—</td><td>—</td><td>—</td><td>—</td><td>—</td>"}</tr>`;
      }).join("")}
      </table>
      <div class="nw-facts" id="nw-apc"></div>`;
    $("stage").appendChild(el);
    // APC summary under the current filters (all selected periods).
    const accAll = (await this.data.combine(this.F)).get(sid);
    if (accAll && (accAll.nDoor ?? 0) > 0) {
      const dd = this.data.doorDateCount(this.F) || 1;
      el.querySelector("#nw-apc").textContent =
        `≈${(accAll.sumOns / dd).toFixed(0)} ons · ${(accAll.sumOffs / dd).toFixed(0)} offs per day here ` +
        `(door data on ${accAll.nDoor} of ${accAll.n} traversals)`;
    }
    el.querySelector(".nw-close").onclick = () => { el.remove(); this._restoreHighlight(); };
    el.querySelector("#nw-rev")?.addEventListener("click", (e) => {
      e.preventDefault();
      this._select(p.rev_sid);
    });
  }
}
