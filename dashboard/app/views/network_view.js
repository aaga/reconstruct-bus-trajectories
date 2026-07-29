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
import { $ } from "../chart_util.js";
import { NetworkMap } from "../network_map.js";
import { StreetViewPopup } from "../street_view.js";
import { State } from "../state.js";
import { deriveStat, cleanLabel } from "../network_data.js";

export const METRICS = {
  overall:          { label: "Overall delay",          kind: "delay", unit: "s" },
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

  async render() {
    if (!this.data) return;
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
            <option value="overall">Overall delay</option>
            <option value="pax">Passenger-weighted delay</option>
            <option value="nondwell">Non-dwell delay</option>
            <option value="dwell">Dwell delay</option>
          </optgroup>
          <optgroup label="Other">
            <option value="freeflow_speed">Free flow speed</option>
            <option value="buses_per_hr">Bus / hour</option>
            <option value="boardings_per_hr">Boardings / hour</option>
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
    if (sid == null) return;
    const f = this.data.segments.features.find((x) => x.properties.sid === sid);
    if (!f) return;
    const p = f.properties;
    this.map.highlight([sid], { zoom: false });

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
      <table class="nw-ptable">
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
      <div class="nw-facts" id="nw-apc"></div>`;
    $("stage").appendChild(el);
    const accAll = (await this.data.combine(this.F)).get(sid);
    if (accAll && (accAll.nDoor ?? 0) > 0) {
      const dd = this.data.doorDateCount(this.F) || 1;
      el.querySelector("#nw-apc").textContent =
        `≈${(accAll.sumOns / dd).toFixed(0)} ons · ${(accAll.sumOffs / dd).toFixed(0)} offs per day here ` +
        `(door data on ${accAll.nDoor} of ${accAll.n} traversals)`;
    }
    el.querySelector(".nw-close").onclick = () => { el.remove(); this.map.highlight([]); };
    el.querySelector("#nw-rev")?.addEventListener("click", (e) => {
      e.preventDefault();
      this._select(p.rev_sid);
    });
  }
}
