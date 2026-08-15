// Network tab: filter panel + segment choropleth + detail panel.
//
// Metric model (2026-07 redesign): a metric FAMILY (Overall / Passenger-
// weighted / Non-dwell / Dwell delay, or Free-flow speed / Bus-per-hour /
// Boardings-per-hour) plus, for delay families, a STAT (mean, median,
// std dev, p95, buffer = p95 − mean). Two mutually-exclusive compare
// toggles: "peak vs off-peak" (Δ = peak − off-peak, forced weekday excl.
// holidays) and "selection vs all routes" (Δ = selected-route traffic −
// all traffic on the selection's segments).
//
// Direction filter activates only for a single selected route. Corridors
// and Areas-of-interest are removed/hidden pending rework.

import * as d3 from "https://cdn.jsdelivr.net/npm/d3@7/+esm";
import { $, showBanner } from "../chart_util.js";
import { NetworkMap } from "../network_map.js";
import { StreetViewPopup } from "../street_view.js";
import { State } from "../state.js";
import { deriveStat, cleanLabel } from "../network_data.js";

export const METRICS = {
  overall:          { label: "Overall delay (t_obs − t_ff)", kind: "delay", unit: "s" },
  pax:              { label: "Passenger-weighted delay", kind: "delay", unit: "pax·s" },
  nondwell:         { label: "Non-dwell delay",        kind: "delay", unit: "s" },
  dwell:            { label: "Dwell delay",            kind: "delay", unit: "s" },
  freeflow_speed:   { label: "Free flow speed",        kind: "static", unit: "mph" },
  buses_per_hr:     { label: "Bus / hour",             kind: "rate", unit: "/hr" },
  boardings_per_hr: { label: "Boardings / hour",       kind: "rate", unit: "/hr" },
};

export const STATS = {
  mean: "mean", median: "median", std: "std dev", p95: "p95", buffer: "buffer (p95−mean)",
};

const PERIOD_LABELS = {
  am_peak: "AM peak", midday: "Midday", pm_peak: "PM peak",
  evening: "Evening", late_night: "Late night",
};
const PERIOD_HOURS = {
  am_peak: "6:00–10:00", midday: "10:00–15:00", pm_peak: "15:00–19:00",
  evening: "19:00–22:00", late_night: "22:00–6:00",
};
const PEAK = ["am_peak", "pm_peak"];
const OFFPEAK = ["midday", "evening", "late_night"];
const ALL_PERIODS = ["am_peak", "midday", "pm_peak", "evening", "late_night"];

function fmtValue(metric, v) {
  const m = METRICS[metric];
  if (m.unit === "mph") return `${v.toFixed(0)}`;
  if (m.unit === "/hr") return v.toFixed(1);
  if (m.unit === "pax·s") {
    return Math.abs(v) >= 1000 ? `${(v / 1000).toFixed(1)}k` : `${v.toFixed(0)}`;
  }
  return `${v.toFixed(0)}s`;
}

export class NetworkView {
  constructor(S) {
    this.S = S;
    this.map = null;
    this._panelBuilt = false;
    this._tooltip = null;
  }

  get data() { return this.S.network.data; }
  get F() { return this.S.network.filters; }
  get N() { return this.S.network; }

  // ---- lifecycle ---------------------------------------------------------

  // Door/APC coverage exists for this city's payloads (drives which
  // metrics/views are available; MBTA has none).
  get hasDoor() {
    return (this.data?.meta?.n_door_dates ?? 0) > 0;
  }

  async render() {
    if (!this.data) return;
    // A door-only metric can arrive via URL or linger from another city.
    if (!this.hasDoor &&
        ["pax", "nondwell", "dwell", "boardings_per_hr"].includes(this.N.metric)) {
      this.N.metric = "overall";
    }
    // Restore a deep-linked selected segment (opens its detail panel).
    // seg= carries a stable seg_id (SIG_<up>__SIG_<down>); numeric values
    // are legacy sid links from before 2026-08-10 and are refused — sids
    // renumber on every registry rebuild.
    if (this.N.pendingSegId != null || this.N.pendingSeg != null) {
      const segId = this.N.pendingSegId;
      const legacySid = this.N.pendingSeg;
      this.N.pendingSegId = null;
      this.N.pendingSeg = null;
      setTimeout(() => {
        if (segId == null) {
          showBanner(`This link uses a legacy numeric segment id (seg=${legacySid}), ` +
                     `which is not stable across data rebuilds. Re-open the segment ` +
                     `and copy a fresh link.`, "warn");
          return;
        }
        const f = this.data.segments.features.find((x) => x.properties.seg_id === segId);
        if (!f) {
          showBanner(`Segment ${segId} is not in the current network build ` +
                     `(its junction cluster may have been re-keyed).`, "warn");
          return;
        }
        const mid = f.geometry.coordinates[Math.floor(f.geometry.coordinates.length / 2)];
        this.map?.map.jumpTo({ center: mid, zoom: 14.5 });
        this._select(f.properties.sid);
      }, 800);
    }
    // Restore route selection deep-linked in the URL (names -> indices).
    if (this.N.pendingRoutes || this.N.pendingActive) {
      const rids = this.data.meta.dims.route_ids;
      if (this.N.pendingRoutes) {
        this.N.checkedRoutes = this.N.pendingRoutes
          .map((r) => rids.indexOf(r)).filter((i) => i >= 0);
      }
      if (this.N.pendingActive) {
        const i = rids.indexOf(this.N.pendingActive);
        this.N.activeRoute = i >= 0 ? i : null;
      }
      this.N.pendingRoutes = this.N.pendingActive = null;
      this.F.routes = this._selectedRouteIdx();
    }
    if (!this._panelBuilt) this._buildPanel();
    if (!this.map) {
      this.map = new NetworkMap($("map"), this.data.segments, {
        onHover: (f, lngLat, point) => this._hover(f, point),
        onClick: (f) => this._select(f ? f.properties.sid : null),
        onContextMenu: (f, lngLat) => this._streetView(f, lngLat),
      });
      this.S.network.map = this.map;
    }
    this._syncPanel();
    await this.refresh();
    setTimeout(() => this.map?.resize(), 60);
  }

  destroy() {
    this._svPopup?.destroy();
    this._svPopup = null;
    this.map?.destroy();
    this.map = null;
    this.S.network.map = null;
    document.querySelector(".nw-panel")?.remove();
    document.querySelector(".nw-detail")?.remove();
    this._tooltip?.remove();
    this._tooltip = null;
    this._panelBuilt = false;
  }

  // ---- filter panel ------------------------------------------------------

  _buildPanel() {
    const dims = this.data.meta.dims;
    const el = document.createElement("div");
    el.className = "nw-panel";
    el.innerHTML = `
      <div class="nw-group"><b>Metric</b>
        <select id="nw-metric">
          <optgroup label="Delays">
            <option value="overall">Overall delay (t_obs − t_ff)</option>
            <option value="pax" ${this.hasDoor ? "" : "disabled"}>Passenger-weighted delay${this.hasDoor ? "" : " (needs door data)"}</option>
            <option value="nondwell" ${this.hasDoor ? "" : "disabled"}>Non-dwell delay${this.hasDoor ? "" : " (needs door data)"}</option>
            <option value="dwell" ${this.hasDoor ? "" : "disabled"}>Dwell delay${this.hasDoor ? "" : " (needs door data)"}</option>
          </optgroup>
          <optgroup label="Other">
            <option value="freeflow_speed">Free flow speed</option>
            <option value="buses_per_hr">Bus / hour</option>
            <option value="boardings_per_hr" ${this.hasDoor ? "" : "disabled"}>Boardings / hour${this.hasDoor ? "" : " (needs door data)"}</option>
          </optgroup>
        </select>
        <span class="nw-radios" id="nw-stats">
          ${Object.entries(STATS).map(([k, lab], i) =>
            `<label><input type="radio" name="nw-stat" value="${k}" ${i === 0 ? "checked" : ""}> ${lab}</label>`).join("")}
        </span>
        <label class="nw-toggle"><input type="checkbox" id="nw-cmp-peak"> Compare peak vs off-peak</label>
      </div>
      <div class="nw-group"><b>Periods</b>
        <span class="nw-quick">
          <button data-q="all">all</button><button data-q="peak">peak</button><button data-q="off">off-peak</button>
        </span>
        <span class="nw-checks" id="nw-periods">
        ${ALL_PERIODS.map((p) =>
          `<label title="${PERIOD_HOURS[p]}"><input type="checkbox" data-p="${p}"> ${PERIOD_LABELS[p]}</label>`).join("")}
        </span>
      </div>
      <div class="nw-group"><b>Days</b>
        <select id="nw-daytype">
          <option value="">everyday</option>
          <option value="weekday" selected>weekday (excl. holidays)</option>
          <option value="weekend">weekend</option>
          <option value="" disabled>──────────</option>
          ${["mon","tue","wed","thu","fri","sat","sun"].map((d, i) =>
            `<option value="dow${i}">${d} only</option>`).join("")}
        </select>
      </div>
      <div class="nw-group"><b>Pick / Weather</b>
        <div class="nw-inline">
          <select id="nw-pick"><option value="">pick: all</option>
            ${dims.picks.map((p, i) => `<option value="${i}">${p}</option>`).join("")}</select>
          <select id="nw-weather"><option value="">weather: any</option>
            ${dims.weathers.filter((w) => w !== "unknown").map((w) =>
              `<option value="${dims.weathers.indexOf(w)}">${w}</option>`).join("")}</select>
        </div>
      </div>
      <div class="nw-group"><b>Routes</b>
        <div class="nw-minirow" style="margin-bottom:4px">
          <input id="nw-route-search" class="nw-search" placeholder="type to filter routes…">
        </div>
        <div class="nw-minirow" style="margin-bottom:4px">
          <button class="nw-smallbtn" id="nw-show-selected" disabled>show selected</button>
          <button class="nw-smallbtn" id="nw-routes-clear" disabled>clear selections</button>
        </div>
        <div class="nw-routelist" id="nw-routelist"></div>
        <label class="nw-toggle"><input type="checkbox" id="nw-cmp-sel" disabled
          title="select at least one route first"> Compare selected route buses vs all buses</label>
      </div>
      <div class="nw-group"><b>Min traversals</b>
        <div class="nw-minirow">
          <input id="nw-minn" type="number" value="${this.N.minN}" min="1" max="10000">
          <span class="nw-note" id="nw-count"></span>
        </div>
      </div>`;
    $("stage").appendChild(el);

    // ---- routes list (checkbox rows; active row + checked stick) ----
    this._routeQuery = "";
    this._showSelectedOnly = false;
    const renderRoutes = () => {
      const host = el.querySelector("#nw-routelist");
      const checked = new Set(this.N.checkedRoutes ?? []);
      if (this._showSelectedOnly && checked.size === 0) {
        this._showSelectedOnly = false;
        el.querySelector("#nw-show-selected").textContent = "show selected";
      }
      const active = this.N.activeRoute;
      host.innerHTML = "";
      const selection = this._selectedRouteIdx();
      const single = selection.length === 1 ? selection[0] : null;
      dims.route_ids.forEach((r, i) => {
        if (this._showSelectedOnly && !checked.has(i)) return;
        if (this._routeQuery && !r.toLowerCase().includes(this._routeQuery)) return;
        const row = document.createElement("div");
        row.className = "nw-routerow" + (active === i ? " active" : "");
        // Direction lives IN the row, only while this route is the single
        // selection; with 2+ routes selected direction is implicitly Both.
        let dirBtns = "";
        if (single === i) {
          const dirs = this._routeDirs(i);
          dirBtns = `<span class="nw-dirbtns">` +
            ["", ...dirs].map((d) =>
              `<button data-d="${d}" class="${(this.F.direction ?? "") === d ? "on" : ""}">` +
              `${d || "Both"}</button>`).join("") + `</span>`;
        }
        row.innerHTML = `<span>${r}</span>${dirBtns}<input type="checkbox" ${checked.has(i) ? "checked" : ""}>`;
        row.querySelectorAll(".nw-dirbtns button").forEach((b) => {
          b.onclick = (e) => {
            e.stopPropagation();
            this.F.direction = b.dataset.d || null;
            renderRoutes();
            this.refresh();
          };
        });
        row.querySelector("input").onclick = (e) => {
          e.stopPropagation();
          const set = new Set(this.N.checkedRoutes ?? []);
          if (e.target.checked) set.add(i); else set.delete(i);
          this.N.checkedRoutes = [...set];
          this._applyRouteSelection();
        };
        row.onclick = () => {
          this.N.activeRoute = this.N.activeRoute === i ? null : i;
          this._applyRouteSelection();
        };
        host.appendChild(row);
      });
    };
    this._renderRoutes = renderRoutes;
    renderRoutes();

    el.querySelector("#nw-route-search").oninput = (e) => {
      this._routeQuery = e.target.value.toLowerCase();
      renderRoutes();
    };
    el.querySelector("#nw-show-selected").onclick = (e) => {
      this._showSelectedOnly = !this._showSelectedOnly;
      e.target.textContent = this._showSelectedOnly ? "show all" : "show selected";
      renderRoutes();
    };
    el.querySelector("#nw-routes-clear").onclick = () => {
      this.N.checkedRoutes = [];
      this.N.activeRoute = null;
      // Snap back to the full list — an empty "selected only" view is a trap.
      this._showSelectedOnly = false;
      el.querySelector("#nw-show-selected").textContent = "show selected";
      renderRoutes();
      this._applyRouteSelection();
    };

    // ---- metric wiring ----
    const metricSel = el.querySelector("#nw-metric");
    metricSel.value = this.N.metric;
    metricSel.onchange = () => {
      this.N.metric = metricSel.value;
      this._syncMetricControls();
      this.refresh();
    };
    el.querySelectorAll('#nw-stats input[name="nw-stat"]').forEach((r) => {
      r.onchange = () => { this.N.stat = r.value; this.refresh(); };
    });
    el.querySelector("#nw-cmp-peak").onchange = (e) => {
      this.N.compare = e.target.checked ? "peak" : null;
      if (e.target.checked) el.querySelector("#nw-cmp-sel").checked = false;
      this._syncMetricControls();
      this.refresh();
    };
    el.querySelector("#nw-cmp-sel").onchange = (e) => {
      this.N.compare = e.target.checked ? "selection" : null;
      if (e.target.checked) el.querySelector("#nw-cmp-peak").checked = false;
      this._syncMetricControls();
      this.refresh();
    };

    // ---- periods ----
    el.querySelectorAll(".nw-quick button").forEach((b) => {
      b.onclick = () => {
        this.F.periods = { all: ALL_PERIODS, peak: PEAK, off: OFFPEAK }[b.dataset.q].slice();
        this._syncPanel();
        this.refresh();
      };
    });
    el.querySelectorAll("#nw-periods input").forEach((cb) => {
      cb.onchange = () => {
        this.F.periods = [...el.querySelectorAll("#nw-periods input:checked")].map((c) => c.dataset.p);
        this.refresh();
      };
    });

    // ---- days / pick / weather ----
    el.querySelector("#nw-daytype").onchange = (e) => {
      const v = e.target.value;
      if (v.startsWith("dow")) { this.F.daytype = null; this.F.dow = Number(v.slice(3)); }
      else { this.F.daytype = v || null; this.F.dow = null; }
      this.refresh();
    };
    for (const [id, key] of [["nw-pick", "pick"], ["nw-weather", "weather"]]) {
      el.querySelector(`#${id}`).onchange = (e) => {
        this.F[key] = e.target.value === "" ? null : Number(e.target.value);
        this.refresh();
      };
    }
    el.querySelector("#nw-minn").onchange = (e) => {
      this.N.minN = Math.max(1, Number(e.target.value) || 1);
      this.refresh();
    };

    this._panelBuilt = true;
    this._syncMetricControls();
  }

  _routeDirs(i) {
    this._dirCache ??= new Map();
    if (!this._dirCache.has(i)) {
      const rid = this.data.meta.dims.route_ids[i];
      const dirs = new Set();
      for (const f of this.data.segments.features) {
        for (const r of f.properties.routes) if (r.r === rid) dirs.add(r.dir);
      }
      this._dirCache.set(i, [...dirs].sort());
    }
    return this._dirCache.get(i);
  }

  // Selected routes = checked set ∪ active row.
  _selectedRouteIdx() {
    const set = new Set(this.N.checkedRoutes ?? []);
    if (this.N.activeRoute != null) set.add(this.N.activeRoute);
    return [...set];
  }

  _applyRouteSelection() {
    this.F.routes = this._selectedRouteIdx();
    if (this.F.routes.length !== 1) this.F.direction = null; // Both
    this._applyRouteButtons();
    this._renderRoutes?.();
    const sel = document.querySelector("#nw-cmp-sel");
    if (sel && sel.disabled && this.N.compare === "selection") {
      sel.checked = false;
      this.N.compare = null;
      this._syncMetricControls();
    }
    this.refresh();
  }

  _syncMetricControls() {
    const isDelay = METRICS[this.N.metric].kind === "delay";
    const stats = document.querySelector("#nw-stats");
    if (stats) stats.style.display = isDelay ? "" : "none";
    // peak compare invalidates periods + days
    const peakCmp = this.N.compare === "peak";
    document.querySelectorAll("#nw-periods input, .nw-quick button").forEach((n) => {
      n.disabled = peakCmp;
    });
    const days = document.querySelector("#nw-daytype");
    if (days) days.disabled = peakCmp;
  }

  _syncPanel() {
    const el = document.querySelector(".nw-panel");
    if (!el) return;
    el.querySelectorAll("#nw-periods input").forEach((cb) => {
      cb.checked = this.F.periods.includes(cb.dataset.p);
    });
    const stat = el.querySelector(`#nw-stats input[value="${this.N.stat}"]`);
    if (stat) stat.checked = true;
    const metricSel = el.querySelector("#nw-metric");
    if (metricSel) metricSel.value = this.N.metric;
    const days = el.querySelector("#nw-daytype");
    if (days) {
      days.value = this.F.dow != null ? `dow${this.F.dow}` : (this.F.daytype ?? "");
    }
    const pick = el.querySelector("#nw-pick");
    if (pick) pick.value = this.F.pick == null ? "" : String(this.F.pick);
    const weather = el.querySelector("#nw-weather");
    if (weather) weather.value = this.F.weather == null ? "" : String(this.F.weather);
    el.querySelector("#nw-cmp-peak").checked = this.N.compare === "peak";
    el.querySelector("#nw-cmp-sel").checked = this.N.compare === "selection";
    const minn = el.querySelector("#nw-minn");
    if (minn) minn.value = this.N.minN;
    this._renderRoutes?.();
    this._applyRouteButtons();
    this._syncMetricControls();
  }

  _applyRouteButtons() {
    const any = this._selectedRouteIdx().length > 0;
    const anyChecked = (this.N.checkedRoutes ?? []).length > 0;
    const showSel = document.querySelector("#nw-show-selected");
    const clearSel = document.querySelector("#nw-routes-clear");
    if (showSel) showSel.disabled = !anyChecked;
    if (clearSel) clearSel.disabled = !any;
    const sel = document.querySelector("#nw-cmp-sel");
    if (sel) sel.disabled = !any;
  }

  // ---- segment visibility under route/direction --------------------------

  _visibleSids() {
    const dims = this.data.meta.dims;
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

  // ---- metric computation ------------------------------------------------

  _minCount(acc) {
    // door-derived families gate on the door-covered subset
    const fam = this.N.metric;
    if (fam === "overall" || fam === "buses_per_hr") return acc.n;
    return acc.nDoor ?? 0;
  }

  async _valuesFor(filters) {
    const metric = this.N.metric;
    const values = new Map();
    this._tFf ??= new Map(
      this.data.segments.features.map((f) => [f.properties.sid, f.properties.t_ff_s]));
    const tFf = this._tFf;

    if (metric === "freeflow_speed") {
      for (const f of this.data.segments.features) {
        const p = f.properties;
        if (p.t_ff_s > 0) values.set(p.sid, (p.len_m / p.t_ff_s) * 2.23694);
      }
      return values;
    }
    const combined = await this.data.combine(filters);
    if (metric === "buses_per_hr" || metric === "boardings_per_hr") {
      const dates = metric === "buses_per_hr"
        ? this.data.dateCount(filters) : this.data.doorDateCount(filters);
      const hours = this.data.periodHours(filters);
      for (const [sid, acc] of combined) {
        if (this._minCount(acc) < this.N.minN) continue;
        if (dates > 0 && hours > 0) {
          values.set(sid, (metric === "buses_per_hr" ? acc.n : acc.sumOns) / (dates * hours));
        }
      }
      return values;
    }
    for (const [sid, acc] of combined) {
      if (this._minCount(acc) < this.N.minN) continue;
      const v = deriveStat(metric, this.N.stat, acc, tFf.get(sid), this.data.meta);
      if (Number.isFinite(v)) values.set(sid, v);
    }
    return values;
  }

  async refresh() {
    this.N.syncHash?.();
    const cnt = document.querySelector("#nw-count");
    if (cnt) cnt.textContent = "loading data…";
    try {
      await this._refreshInner();
    } catch (err) {
      console.error("network refresh failed", err);
      if (cnt) cnt.textContent = `load failed: ${err.message || err}`;
    }
  }

  async _refreshInner() {
    const metric = this.N.metric;
    const spec = METRICS[metric];
    const visible = this._visibleSids();
    let values;
    let diverging = false;
    let legendTitle = spec.label + (spec.kind === "delay" ? ` · ${STATS[this.N.stat]}` : "");

    if (this.N.compare === "peak" && spec.kind !== "static") {
      diverging = true;
      legendTitle += " · peak − off-peak (weekday)";
      const base = { ...this.F, daytype: "weekday", dow: null };
      const [pk, off] = await Promise.all([
        this._valuesFor({ ...base, periods: PEAK }),
        this._valuesFor({ ...base, periods: OFFPEAK }),
      ]);
      values = new Map();
      for (const [sid, v] of pk) {
        const o = off.get(sid);
        if (o !== undefined) values.set(sid, v - o);
      }
    } else if (this.N.compare === "selection" && this.F.routes.length && spec.kind !== "static") {
      diverging = true;
      legendTitle += " · selection − all routes";
      const [sel, all] = await Promise.all([
        this._valuesFor(this.F),
        this._valuesFor({ ...this.F, routes: [] }),
      ]);
      values = new Map();
      for (const [sid, v] of sel) {
        const a = all.get(sid);
        if (a !== undefined) values.set(sid, v - a);
      }
    } else {
      values = await this._valuesFor(this.F);
    }

    this._lastValues = values;
    this._paint(values, visible, diverging, legendTitle);
    const nShown = visible
      ? [...values.keys()].filter((s) => visible.has(s)).length
      : values.size;
    const cnt = document.querySelector("#nw-count");
    if (cnt) cnt.textContent = `${nShown} segments`;
  }

  _paint(values, visible, diverging, legendTitle) {
    const metric = this.N.metric;
    const vals = [...values.entries()]
      .filter(([sid]) => !visible || visible.has(sid))
      .map(([, v]) => v);
    if (!vals.length) {
      this.map.setColors(new Map(), visible);
      this.map.setLegend({ title: legendTitle, gradient: ["#ddd", "#ddd"], ticks: ["no data", ""], note: "" });
      return;
    }
    const sorted = vals.slice().sort(d3.ascending);
    const lo = d3.quantile(sorted, 0.02);
    const hi = d3.quantile(sorted, 0.98);

    let colorOf, gradient;
    if (diverging) {
      const m = Math.max(Math.abs(lo), Math.abs(hi)) || 1;
      const sc = d3.scaleSequential(d3.interpolateRdBu).domain([m, -m]); // red = worse
      colorOf = (v) => sc(v);
      gradient = d3.range(0, 1.01, 0.1).map((t) => sc.interpolator()(1 - t));
    } else if (metric === "freeflow_speed") {
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
    this.map.setColors(colors, visible);
    const doorNote = ["pax", "nondwell", "dwell", "boardings_per_hr"].includes(metric)
      ? ` · door data: ${this.data.meta.n_door_dates ?? "?"} of ${this.data.meta.n_dates} days`
      : "";
    const sign = (v) => (diverging && v > 0 ? "+" : "") + fmtValue(metric, v);
    this.map.setLegend({
      title: legendTitle,
      gradient,
      ticks: [sign(lo), sign((lo + hi) / 2), sign(hi)],
      note: `p2–p98 across shown segments · grey = n < ${this.N.minN}${doorNote}`,
    });
  }

  // ---- hover tooltip + detail panel --------------------------------------

  _streetView(f, lngLat) {
    this._svPopup ??= new StreetViewPopup({ shape: null }, new State());
    let heading = 0;
    let title = "Street View";
    if (f) {
      // bearing of the nearest geometry edge, in the segment's travel direction
      const coords = f.geometry?.coordinates;
      if (coords && coords.length >= 2) {
        let best = 0;
        let bestD = Infinity;
        for (let i = 0; i < coords.length - 1; i++) {
          const mx = (coords[i][0] + coords[i + 1][0]) / 2;
          const my = (coords[i][1] + coords[i + 1][1]) / 2;
          const d = (mx - lngLat.lng) ** 2 + (my - lngLat.lat) ** 2;
          if (d < bestD) { bestD = d; best = i; }
        }
        const [lon0, lat0] = coords[best];
        const [lon1, lat1] = coords[best + 1];
        const mlat = Math.cos((lat0 * Math.PI) / 180);
        heading = (Math.atan2((lon1 - lon0) * mlat, lat1 - lat0) * 180) / Math.PI;
        heading = (heading + 360) % 360;
      }
      title = cleanLabel(f.properties.label ?? "Street View");
    }
    this._svPopup.openAt(lngLat.lat, lngLat.lng, heading, title);
  }

  // ---- delay-location distribution (road strip + stacked bars) -----------
  // Locations are meters upstream of the DOWNSTREAM signal; the traffic
  // light sits at the LEFT and the road extends right (travel is right-to-
  // left, toward the light). Classes: red = non-dwell events, turquoise =
  // pre-boarding dwell (>10 s before door open), purple = post-boarding
  // (>10 s after door close). NOTE: uses strict event classification — NOT
  // the same as the (time-subtraction) map metrics until the event batch
  // redefines them.
  async _renderDistribution(host, props, coords) {
    let d;
    try {
      const r = await fetch(`${this.data.base}/dist/${props.sid}.json`);
      if (!r.ok) throw new Error();
      d = await r.json();
    } catch {
      host.innerHTML = `<div class="nw-note">no delay-event data for this segment yet</div>`;
      return;
    }
    // Stale-tab guard: dist files are stamped with the intersections-cache
    // sha they were built against; a mismatch with the meta loaded at page
    // start means the network data was rebuilt under this tab.
    const metaSha = this.data.meta?.intersections_sha256;
    if (d.sha && metaSha && !metaSha.startsWith(d.sha)) {
      showBanner("Network data was rebuilt since this page loaded — " +
                 "reload the page to get consistent segments.", "warn");
      host.innerHTML = `<div class="nw-note">data version mismatch — reload the page</div>`;
      return;
    }
    this._distMode ??= "events"; // "events" | "seconds" | "queue" | "pings"
    if (this._distMode === "pings" && !d.ping) this._distMode = "events";
    const pingMode = this._distMode === "pings";
    const secondsMode = this._distMode === "seconds";
    const queueMode = this._distMode === "queue";
    const suffix = secondsMode ? "_s" : queueMode ? "_q" : "";
    // v2 classes; v1 files simply lack post2/dw (empty fallbacks keep them
    // working). dw (door events) is opt-in via the checkbox.
    this._showDoors ??= false;
    const CLASSES = pingMode ? ["ping"]
                   : ["nd", "pre", "post", "post2",
                      ...(this._showDoors ? ["dw"] : [])];
    // Turn-movement filter (T/L/R/E through the downstream signal).
    // Selection resets when the popup moves to a different segment.
    const mvmtAll = d.mvmt ? Object.keys(d.mvmt) : [];
    if (this._mvmtSeg !== props.sid) {
      this._mvmtSeg = props.sid;
      this._mvmtSel = new Set(mvmtAll);
    }
    const mvmtFiltered = !pingMode && d.by_mvmt && this._mvmtSel.size < mvmtAll.length;
    // Seconds mode: divide by the traversal count so the y-axis reads as
    // AVERAGE delay seconds per trip (dumb rescale, per user). When the
    // movement filter is active, both the arrays and the denominator come
    // from the selected movements only.
    const selTrips = mvmtFiltered
      ? [...this._mvmtSel].reduce((a, m) => a + (d.mvmt[m] ?? 0), 0)
      : (d.n_trips ?? 1);
    const denom = secondsMode ? Math.max(1, selTrips) : 1;
    const zeros = d.nd.map(() => 0);
    const pick = (c) => {
      if (mvmtFiltered) {
        const out = zeros.slice();
        for (const m of this._mvmtSel) {
          const arr = d.by_mvmt[m]?.[c + suffix] ?? d.by_mvmt[m]?.[c];
          if (arr) arr.forEach((v, i) => { out[i] += v; });
        }
        return out.map((v) => v / denom);
      }
      return (d[c + suffix] ?? d[c] ?? zeros).map((v) => v / denom);
    };
    const src = Object.fromEntries(CLASSES.map((c) => [c, pick(c)]));
    const W = 990, chartH = 270, roadH = 64, axisH = 30, padL = 58, padR = 28;
    const H = chartH + roadH + axisH + 52;
    const lenFt = d.len_ft;
    const nB = d.nd.length;
    const innerW = W - padL - padR;
    // Ghost zones: 10% of segment length beyond each end, from the adjacent
    // segments' events (build_distributions gh_lo/gh_hi). Extends the x
    // domain; 0 ft (the light) stays the segment's left edge.
    const ghB = (d.ghost_buckets && (d.gh_lo || d.gh_hi)) ? d.ghost_buckets : 0;
    const ghFt = ghB * d.bucket_ft;
    const domFt = lenFt + 2 * ghFt;
    const xOf = (ft) => padL + ((ft + ghFt) / domFt) * innerW;
    const bw = Math.max(1, (d.bucket_ft / domFt) * innerW);

    const totals = src[CLASSES[0]].map((_, i) =>
      CLASSES.reduce((a, c) => a + src[c][i], 0));
    const yMax = Math.max(1, ...totals);
    const yOf = (n) => chartH - (n / yMax) * (chartH - 6);

    const COLORS = { nd: "#d63a2f", pre: "#1fb8b0",
                     post: "#8a4fc8", post2: "url(#post2hatch)",
                     dw: "#2b6fd6", ping: "#3f3f3f" };
    let bars = `<defs><pattern id="post2hatch" width="6" height="6"
        patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
        <rect width="6" height="6" fill="#8a4fc8"/>
        <line x1="0" y1="0" x2="0" y2="6" stroke="#fff" stroke-width="2.2"/>
      </pattern></defs>`;
    for (let i = 0; i < nB; i++) {
      let y = chartH;
      for (const cls of CLASSES) {
        const v = src[cls][i];
        if (!v) continue;
        const h = ((v / yMax) * (chartH - 6));
        y -= h;
        bars += `<rect x="${xOf(i * d.bucket_ft).toFixed(1)}" y="${y.toFixed(1)}"
                 width="${bw.toFixed(1)}" height="${h.toFixed(1)}" fill="${COLORS[cls]}"/>`;
      }
    }
    // Ghost bars: adjacent segments' events at 50% opacity, clipped to the
    // main chart's y-scale (they're context, not part of it).
    if (ghB && !pingMode) {
      const pickGh = (side, c) => {
        const key = c + suffix;
        const out = new Array(ghB).fill(0);
        if (mvmtFiltered) {
          for (const m of this._mvmtSel) {
            const a = d.by_mvmt[m]?.[side]?.[key] ?? d.by_mvmt[m]?.[side]?.[c];
            if (a) a.forEach((v, i) => { out[i] += v; });
          }
        } else {
          const a = d[side]?.[key] ?? d[side]?.[c];
          if (a) a.forEach((v, i) => { out[i] += v; });
        }
        return out.map((v) => v / denom);
      };
      let ghost = "";
      for (const [side, base] of [["gh_lo", -ghFt], ["gh_hi", lenFt]]) {
        const srcG = Object.fromEntries(CLASSES.map((c) => [c, pickGh(side, c)]));
        for (let i = 0; i < ghB; i++) {
          let y = chartH;
          for (const cls of CLASSES) {
            const v = srcG[cls][i];
            if (!v) continue;
            let h = ((v / yMax) * (chartH - 6));
            if (y - h < 8) h = y - 8;          // clip the stack at the top
            if (h <= 0) break;
            y -= h;
            ghost += `<rect x="${xOf(base + i * d.bucket_ft).toFixed(1)}" y="${y.toFixed(1)}"
                     width="${bw.toFixed(1)}" height="${h.toFixed(1)}" fill="${COLORS[cls]}"/>`;
          }
        }
      }
      if (ghost) bars += `<g opacity="0.5">${ghost}</g>`;
    }

    // y axis: 0, mid, max
    const fmtY = (v) => secondsMode
      ? (v >= 60 ? `${(v / 60).toFixed(1)}m` : `${v.toFixed(1)}s`)
      : String(Math.round(v));
    const yAxis = `
      <line x1="${padL - 4}" y1="8" x2="${padL - 4}" y2="${chartH}" stroke="#999"/>
      <text x="${padL - 8}" y="16" text-anchor="end" class="dist-tick">${fmtY(yMax)}</text>
      <text x="${padL - 8}" y="${(chartH + 16) / 2}" text-anchor="end" class="dist-tick">${fmtY(yMax / 2)}</text>
      <text x="${padL - 8}" y="${chartH}" text-anchor="end" class="dist-tick">0</text>`;

    // road strip
    const roadY = chartH + 8;
    const roadBodyH = roadH - 18;
    const axisY = roadY + roadBodyH + 52;
    const sideY = axisY - 2;   // sideways labels: leading edge at the x-axis
    // Break the roadway (12 px gaps) at NAMED junctions — non-signalized
    // by definition (signals are segment boundaries); label each gap.
    const namedJcts = (props.junctions_off ?? [])
      .filter((j) => j.cross)
      .map((j) => ({ x: xOf(j.off_m * 3.28084), cross: j.cross }))
      .filter((j) => j.x > xOf(0) + 18 && j.x < xOf(lenFt) - 18)
      .sort((a, b) => a.x - b.x);
    let road = "";
    {
      const cy = roadY + roadBodyH / 2;
      let cursor = xOf(0);
      const spans = [];
      for (const j of namedJcts) {
        spans.push([cursor, j.x - 6]);
        cursor = j.x + 6;
      }
      spans.push([cursor, xOf(lenFt)]);
      for (const [a, b] of spans) {
        if (b - a < 2) continue;
        road += `<rect x="${a.toFixed(1)}" y="${roadY}" width="${(b - a).toFixed(1)}"
                 height="${roadBodyH}" rx="6" fill="#4a4a52"/>
          <line x1="${(a + 4).toFixed(1)}" y1="${cy}" x2="${(b - 4).toFixed(1)}" y2="${cy}"
                stroke="#fff" stroke-width="2.5" stroke-dasharray="16 13" opacity=".7"/>`;
      }
      for (const j of namedJcts) {
        const nm = j.cross.replace(/^(North|South|East|West) /, "");
        road += `<text transform="rotate(-90 ${j.x.toFixed(1)} ${sideY})" x="${j.x.toFixed(1)}"
                 y="${sideY}" text-anchor="start"
                 style="font-size:8px;fill:#555" dominant-baseline="middle">${nm}</text>`;
      }
    }
    // travel direction: a few left-pointing chevrons on the centerline
    {
      const cy = roadY + roadBodyH / 2;
      for (const frac of [0.22, 0.5, 0.78]) {
        const ax = xOf(lenFt * frac);
        road += `<path d="M ${(ax + 7).toFixed(1)} ${cy - 6} L ${(ax - 5).toFixed(1)} ${cy}
                 L ${(ax + 7).toFixed(1)} ${cy + 6}" fill="none" stroke="#fff"
                 stroke-width="3" stroke-linecap="round" stroke-linejoin="round" opacity=".95"/>`;
      }
      // street name on the roadway: pick the position that avoids bus-stop
      // bars (hard) and junction gaps (soft), preferring the center
      if (props.name) {
        const halfW = props.name.length * 3.1 + 6;
        const stopsX = (props.stops_off ?? []).map((st) => xOf(st.off_m * 3.28084));
        const lo = xOf(lenFt) + halfW + 4, hi = xOf(0) - halfW - 4;
        let best = (xOf(0) + xOf(lenFt)) / 2, bestScore = Infinity;
        for (let k = 0; k <= 60; k++) {
          const cx2 = lo + ((hi - lo) * k) / 60;
          let score = Math.abs(cx2 - (xOf(0) + xOf(lenFt)) / 2) / 1000;
          for (const sx of stopsX) {
            if (Math.abs(cx2 - sx) < halfW + 14) score += 100;
          }
          for (const j of namedJcts) {
            if (Math.abs(cx2 - j.x) < halfW + 8) score += 10;
          }
          if (score < bestScore) { bestScore = score; best = cx2; }
        }
        road += `<text x="${best.toFixed(1)}" y="${roadY + roadBodyH - 6}"
                 text-anchor="middle" style="font-size:10px;fill:#fff;opacity:.92;
                 font-weight:600;letter-spacing:.06em">${props.name}</text>`;
      }
    }
    // traffic light pictogram at the segment's left edge (0 ft)
    const ly = roadY + roadBodyH / 2;
    road += `<g transform="translate(${(xOf(0) - 34).toFixed(1)}, ${ly - 22})">
        <rect x="0" y="0" width="19" height="44" rx="4" fill="#222"/>
        <circle cx="9.5" cy="9" r="5" fill="#e33"/>
        <circle cx="9.5" cy="22" r="5" fill="#fb3"/>
        <circle cx="9.5" cy="35" r="5" fill="#3c4"/>
      </g>`;
    const ownStreet = (props.name ?? "").replace(/^(North|South|East|West) /, "");
    // "Street: A → B" — A = upstream boundary cross street, B = downstream
    {
      const ends = ((props.label ?? "").split(":")[1] ?? "").split("→")
        .map((t) => t.trim());
      const cname = (t) => (!t || /mid-block/i.test(t) || t === props.name)
        ? null : t.replace(/^(North|South|East|West) /, "");
      const downName = cname(ends[1]);
      const upName = cname(ends[0]);
      const sideLabel = (tx, name, fill = "#555") =>
        `<text transform="rotate(-90 ${tx} ${sideY})" x="${tx}" y="${sideY}"
         text-anchor="start" style="font-size:8px;fill:${fill}">${name}</text>`;
      if (downName) road += sideLabel((xOf(0) - 42).toFixed(1), downName);
      if (upName) road += sideLabel((xOf(lenFt) + 14).toFixed(1), upName);
    }
    // NB (2026-07-30): junction boxes removed from this view — the way-split
    // fallback produced nameless phantom boxes (alley splits, splits at
    // far-side crossing nodes) that misread as real intersections next to
    // stop bars. Stop-sign octagons remain (real OSM-tagged controls).
    const stopSigns = (props.stop_signs_off ?? []).map((ss) =>
      typeof ss === "number" ? { off_m: ss, cross: null } : ss);
    const junctionTip = (cross) => cross
      ? `${ownStreet} & ${cross.replace(/^(North|South|East|West) /, "")}`
      : `${ownStreet} — cross street`;
    for (const ss of stopSigns) {
      const x = xOf(ss.off_m * 3.28084);
      const tip = junctionTip(ss.cross);
      const r = 9, oy = roadY + roadBodyH / 2;
      const oct = Array.from({length: 8}, (_, i) => {
        const a = (Math.PI / 8) + (i * Math.PI) / 4;
        return `${(x + r * Math.cos(a)).toFixed(1)},${(oy + r * Math.sin(a)).toFixed(1)}`;
      }).join(" ");
      road += `<g data-tip="${tip}"><polygon points="${oct}" fill="#c22"
               stroke="#fff" stroke-width="1.5"/>
               <text x="${x}" y="${oy + 2.5}" text-anchor="middle"
                 style="font-size:6.5px;fill:#fff;font-weight:700">STOP</text></g>`;
    }
    // stops: CTA-style sign bars (bus glyph + BUS STOP) with id · name
    // printed below the roadway, staggered on two rows to limit collisions
    {
      const sorted = [...(props.stops_off ?? [])].sort((a, b) => b.off_m - a.off_m);
      sorted.forEach((st, si) => {
        const x = xOf(st.off_m * 3.28084);
        const top = roadY - 8, bh = roadBodyH + 16;
        road += `<g>
          <rect x="${(x - 11).toFixed(1)}" y="${top}" width="22" height="${bh}"
                rx="4" fill="#2f6fd6" stroke="#fff" stroke-width="1.2"/>
          <g transform="translate(${(x - 6).toFixed(1)}, ${top + 4})" fill="#fff">
            <rect x="0.5" y="1.5" width="11" height="7" rx="1.6"/>
            <rect x="1.8" y="2.8" width="3.2" height="2.4" rx="0.6" fill="#2f6fd6"/>
            <rect x="6.4" y="2.8" width="3.2" height="2.4" rx="0.6" fill="#2f6fd6"/>
            <circle cx="3" cy="9.4" r="1.3"/>
            <circle cx="9" cy="9.4" r="1.3"/>
          </g>
          <text x="${x.toFixed(1)}" y="${top + 24}" text-anchor="middle"
                style="font-size:5.6px;fill:#fff;font-weight:700;letter-spacing:.04em">BUS</text>
          <text x="${x.toFixed(1)}" y="${top + 31}" text-anchor="middle"
                style="font-size:5.6px;fill:#fff;font-weight:700;letter-spacing:.04em">STOP</text>
          <text x="${x.toFixed(1)}" y="${roadY + roadBodyH + (si % 2 ? 37 : 14)}"
                text-anchor="middle" style="font-size:9.5px;fill:#333;font-weight:600">${st.id}</text>
          <text x="${x.toFixed(1)}" y="${roadY + roadBodyH + (si % 2 ? 48 : 25)}"
                text-anchor="middle" style="font-size:9.5px;fill:#333">${st.name}</text>
        </g>`;
      });
    }
    // feet scale
    const step = lenFt > 2000 ? 500 : lenFt > 800 ? 200 : 100;
    let axis = `<line x1="${padL}" y1="${axisY}" x2="${W - padR}" y2="${axisY}" stroke="#999"/>`;
    for (let ft = 0; ft <= lenFt; ft += step) {
      const x = xOf(ft);
      axis += `<line x1="${x}" y1="${axisY}" x2="${x}" y2="${axisY + 6}" stroke="#999"/>
        <text x="${x}" y="${axisY + 20}" text-anchor="middle" class="dist-tick">${ft} ft</text>`;
    }
    // Hover cursor: vertical dotted line + exact distance readout on the
    // axis (pointer-events:none so stop/sign tooltips still fire).
    axis += `<g class="dist-cursor" style="display:none;pointer-events:none">
        <line y1="8" y2="${axisY}" stroke="#333" stroke-width="1"
              stroke-dasharray="3 4"/>
        <rect y="${axisY + 8}" width="58" height="16" rx="4" fill="#333"/>
        <text y="${axisY + 20}" text-anchor="middle" class="dist-tick"
              style="fill:#fff;font-weight:600"></text>
      </g>`;

    host.innerHTML = `
      <hr class="dist-rule">
      <div class="dist-head">${this.hasDoor
          ? "Distribution of non-boarding delays"
          : "Distribution of delay locations"}
        <span class="dist-toggle">
          <button data-m="events" class="${this._distMode === "events" ? "on" : ""}">delay events</button>
          <button data-m="seconds" class="${this._distMode === "seconds" ? "on" : ""}">avg delay seconds</button>
          ${d.nd_q ? `<button data-m="queue" class="${this._distMode === "queue" ? "on" : ""}">last stop</button>` : ""}
          ${d.ping ? `<button data-m="pings" class="${pingMode ? "on" : ""}">raw pings</button>` : ""}
        </span>
        ${this.hasDoor && d.dw && !pingMode ? `<label class="dist-doors" style="font-size:11px;margin-left:10px;cursor:pointer">
          <input type="checkbox" class="dist-doors-cb" ${this._showDoors ? "checked" : ""}> door events</label>` : ""}
        ${mvmtAll.length && !pingMode ? `<span class="dist-mvmt" style="margin-left:10px;display:inline-flex;gap:3px;vertical-align:middle">
          ${mvmtAll.map((m) => {
            const ARROWS = {
              T: "M12 19 V7 M7.5 10.5 L12 5.5 L16.5 10.5",
              L: "M16 19 V13 Q16 10.5 13.5 10.5 H9 M11.5 6.5 L7 10.5 L11.5 14.5",
              R: "M8 19 V13 Q8 10.5 10.5 10.5 H15 M12.5 6.5 L17 10.5 L12.5 14.5",
              E: "M12 19 V9 M7 7 H17",
            };
            const TITLES = { T: "thru", L: "left turn", R: "right turn", E: "route ends" };
            const on = mvmtAll.length === 1 || this._mvmtSel.has(m);
            const interactive = mvmtAll.length > 1 && d.by_mvmt;
            return `<svg class="mvmt-chip" data-m="${m}" width="22" height="22" viewBox="0 0 24 24"
              style="background:${on ? "#2e3440" : "#c9c9c9"};border-radius:4px;${interactive ? "cursor:pointer" : ""}">
              <title>${TITLES[m]} (${d.mvmt[m] ?? 0} trips)</title>
              <path d="${ARROWS[m]}" fill="none" stroke="#fff" stroke-width="2.4"
                    stroke-linecap="round" stroke-linejoin="round"/></svg>`;
          }).join("")}</span>` : ""}
        <span class="nw-note">${pingMode
          ? `(${totals.reduce((a, v) => a + v, 0).toLocaleString()} raw AVL pings, pre-reconstruction)`
          : `(${d.n_events} events · ${d.n_trips ?? "?"} trips)`}</span>
      </div>
      <svg viewBox="0 0 ${W} ${H}" class="dist-svg">${yAxis}${bars}${road}${axis}</svg>
      ${this.hasDoor && !pingMode ? `<div class="dist-legend">
        <span><i style="background:#d63a2f"></i>non-dwell</span>
        <span><i style="background:#1fb8b0"></i>pre-boarding</span>
        <span><i style="background:#8a4fc8"></i>post-boarding</span>
        <span><i style="background:repeating-linear-gradient(45deg,#8a4fc8,#8a4fc8 3px,#fff 3px,#fff 5px)"></i>post-boarding, extra door cycles</span>
        ${this._showDoors ? `<span><i style="background:#2b6fd6"></i>door events</span>` : ""}
      </div>` : ""}`;
    host.querySelectorAll(".dist-toggle button").forEach((b) => {
      b.onclick = () => { this._distMode = b.dataset.m; this._renderDistribution(host, props, coords); };
    });
    const doorsCb = host.querySelector(".dist-doors-cb");
    if (doorsCb) doorsCb.onchange = () => {
      this._showDoors = doorsCb.checked;
      this._renderDistribution(host, props, coords);
    };
    if (mvmtAll.length > 1 && d.by_mvmt) {
      host.querySelectorAll(".mvmt-chip").forEach((chip) => {
        chip.onclick = () => {
          const m = chip.dataset.m;
          if (this._mvmtSel.has(m)) {
            if (this._mvmtSel.size > 1) this._mvmtSel.delete(m); // keep >=1
          } else {
            this._mvmtSel.add(m);
          }
          this._renderDistribution(host, props, coords);
        };
      });
    }
    // Hover cursor: track mouse x, snap the dotted line + axis readout.
    const svg = host.querySelector(".dist-svg");
    {
      const cur = svg.querySelector(".dist-cursor");
      const [curLine, curBox, curText] = [
        cur.querySelector("line"), cur.querySelector("rect"), cur.querySelector("text")];
      svg.addEventListener("mousemove", (e) => {
        const rect = svg.getBoundingClientRect();
        // CSS may scale the svg; convert client px -> viewBox units.
        const px = (e.clientX - rect.left) * (W / rect.width);
        const ft = ((px - padL) / innerW) * domFt - ghFt;
        if (ft < 0 || ft > lenFt) { cur.style.display = "none"; return; }
        cur.style.display = "";
        curLine.setAttribute("x1", px.toFixed(1));
        curLine.setAttribute("x2", px.toFixed(1));
        curBox.setAttribute("x", (px - 29).toFixed(1));
        curText.setAttribute("x", px.toFixed(1));
        curText.textContent = `${Math.round(ft)} ft`;
      });
      svg.addEventListener("mouseleave", () => { cur.style.display = "none"; });
    }
    // Right-click anywhere on the strip -> Street View at that spot along
    // the segment, camera facing the traffic light (travel direction).
    svg.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      const rect = svg.getBoundingClientRect();
      const px = e.clientX - rect.left;
      const ft = ((px - padL) / innerW) * lenFt;
      if (ft < 0 || ft > lenFt || !coords || coords.length < 2) return;
      const offM = ft / 3.28084;                 // upstream of the light
      const fromStartM = Math.max(0, props.len_m - offM);
      // walk the geometry (travel-oriented) to that arc length
      const mlat = Math.cos((coords[0][1] * Math.PI) / 180);
      const segLen = (a, b) =>
        Math.hypot((b[0] - a[0]) * 111320 * mlat, (b[1] - a[1]) * 111320);
      let geomLen = 0;
      for (let i = 0; i < coords.length - 1; i++) geomLen += segLen(coords[i], coords[i + 1]);
      let target = (fromStartM / props.len_m) * geomLen;
      let lat = coords[0][1], lon = coords[0][0], heading = 0;
      for (let i = 0; i < coords.length - 1; i++) {
        const L = segLen(coords[i], coords[i + 1]);
        if (target <= L || i === coords.length - 2) {
          const t = L > 0 ? Math.min(1, target / L) : 0;
          lon = coords[i][0] + t * (coords[i + 1][0] - coords[i][0]);
          lat = coords[i][1] + t * (coords[i + 1][1] - coords[i][1]);
          heading = (Math.atan2(
            (coords[i + 1][0] - coords[i][0]) * mlat,
            coords[i + 1][1] - coords[i][1]) * 180) / Math.PI;
          heading = (heading + 360) % 360;   // travel direction = toward the light
          break;
        }
        target -= L;
      }
      this._svPopup ??= new StreetViewPopup({ shape: null }, new State());
      this._svPopup.openAt(lat, lon, heading,
        `${Math.round(ft)} ft from signal · ${cleanLabel(props.label)}`);
    });
    // Instant tooltips (native <title> has a hover delay).
    const tipEl = document.createElement("div");
    tipEl.className = "nw-tooltip hidden";
    document.body.appendChild(tipEl);
    host._tipEl?.remove();
    host._tipEl = tipEl;
    host.querySelectorAll("[data-tip]").forEach((n) => {
      n.addEventListener("mouseenter", (e) => {
        tipEl.textContent = n.dataset.tip;
        tipEl.classList.remove("hidden");
      });
      n.addEventListener("mousemove", (e) => {
        tipEl.style.left = `${e.clientX + 12}px`;
        tipEl.style.top = `${e.clientY + 12}px`;
      });
      n.addEventListener("mouseleave", () => tipEl.classList.add("hidden"));
    });
  }

  _hover(f, point) {
    if (!this._tooltip) {
      this._tooltip = document.createElement("div");
      this._tooltip.className = "nw-tooltip hidden";
      document.body.appendChild(this._tooltip);
    }
    if (!f) {
      this._tooltip.classList.add("hidden");
      return;
    }
    const p = f.properties;
    const routes = typeof p.routes === "string" ? JSON.parse(p.routes) : p.routes;
    const v = this._lastValues?.get(p.sid);
    const spec = METRICS[this.N.metric];
    const statBit = spec.kind === "delay" ? ` (${STATS[this.N.stat]})` : "";
    this._tooltip.innerHTML = `
      <b>${cleanLabel(p.label)}</b><br>
      routes: ${routes.map((r) => `${r.r} ${r.dir}`).join(", ")}<br>
      ${spec.label}${statBit}: ${v == null ? "—" : fmtValue(this.N.metric, v)} · ${Math.round(p.len_m)} m`;
    const mapRect = $("map").getBoundingClientRect();
    this._tooltip.style.left = `${mapRect.left + point.x + 12}px`;
    this._tooltip.style.top = `${mapRect.top + point.y + 12}px`;
    this._tooltip.classList.remove("hidden");
  }

  async _select(sid) {
    document.querySelector(".nw-detail")?.remove();
    this.S.network.selected = sid;
    this.N.syncHash?.();
    if (sid == null) return;
    const f = this.data.segments.features.find((x) => x.properties.sid === sid);
    if (!f) return;
    const p = f.properties;
    this.map.highlight([sid], { zoom: false });
    // Auto-frame: detail panel covers the left half, so fit the segment
    // into ~80% of the RIGHT half of the map viewport.
    {
      const cs = f.geometry.coordinates;
      const lons = cs.map((c) => c[0]), lats = cs.map((c) => c[1]);
      const rect = $("map").getBoundingClientRect();
      this.map.map.fitBounds(
        [[Math.min(...lons), Math.min(...lats)],
         [Math.max(...lons), Math.max(...lats)]],
        { padding: { left: rect.width * 0.55, right: rect.width * 0.05,
                     top: rect.height * 0.10, bottom: rect.height * 0.10 },
          maxZoom: 17.5, duration: 700 });
    }

    // Per-period table under the current non-period filters.
    const rows = [];
    for (const period of ALL_PERIODS) {
      const acc = (await this.data.combine({ ...this.F, periods: [period] })).get(sid);
      rows.push(acc ?? null);
    }
    const M = this.data.meta;
    const cell = (v, suffix = "") => (Number.isFinite(v) ? `${v.toFixed(0)}${suffix}` : "—");

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
      <button id="nw-ptable-toggle" class="nw-clear" style="margin:2px 0 4px">show delay / dwell table ▾</button>
      <table class="nw-ptable" style="display:none">
        <tr><th></th><th>n</th><th>delay med</th><th>dwell</th><th>non-dwell</th><th>pax·s</th></tr>
        ${ALL_PERIODS.map((per, i) => {
          const acc = rows[i];
          if (!acc || !acc.n) return `<tr><td>${PERIOD_LABELS[per]}</td><td>—</td><td>—</td><td>—</td><td>—</td><td>—</td></tr>`;
          const med = deriveStat("overall", "median", acc, p.t_ff_s, M);
          const dw = deriveStat("dwell", "mean", acc, p.t_ff_s, M);
          const nd = deriveStat("nondwell", "mean", acc, p.t_ff_s, M);
          const px = deriveStat("pax", "mean", acc, p.t_ff_s, M);
          return `<tr><td>${PERIOD_LABELS[per]}</td><td>${acc.n}</td>
            <td>${cell(med, "s")}</td><td>${cell(dw, "s")}</td><td>${cell(nd, "s")}</td><td>${cell(px)}</td></tr>`;
        }).join("")}
      </table>
      <div class="nw-facts" id="nw-apc"></div>
      <div id="nw-dist"></div>`;
    $("stage").appendChild(el);
    this._renderDistribution(el.querySelector("#nw-dist"), p, f.geometry.coordinates);
    const accAll = (await this.data.combine(this.F)).get(sid);
    if (accAll && (accAll.nDoor ?? 0) > 0) {
      const dd = this.data.doorDateCount(this.F) || 1;
      el.querySelector("#nw-apc").textContent =
        `≈${(accAll.sumOns / dd).toFixed(0)} ons · ${(accAll.sumOffs / dd).toFixed(0)} offs per day here ` +
        `(door data on ${accAll.nDoor} of ${accAll.n} traversals)`;
    } else if (!this.hasDoor) {
      el.querySelector("#nw-apc").textContent =
        "no door/APC data available for this city — dwell, non-dwell, " +
        "passenger-weighted, and boardings metrics are unavailable";
    }
    {
      const tbtn = el.querySelector("#nw-ptable-toggle");
      const tbl = el.querySelector(".nw-ptable");
      tbtn.onclick = () => {
        const open = tbl.style.display !== "none";
        tbl.style.display = open ? "none" : "";
        tbtn.textContent = open ? "show delay / dwell table ▾"
                                : "hide delay / dwell table ▴";
      };
    }
    el.querySelector(".nw-close").onclick = () => {
      el.remove();
      this.map.highlight([]);
      this.S.network.selected = null;
      this.N.syncHash?.();
    };
    el.querySelector("#nw-rev")?.addEventListener("click", (e) => {
      e.preventDefault();
      this._select(p.rev_sid);
    });
  }
}
