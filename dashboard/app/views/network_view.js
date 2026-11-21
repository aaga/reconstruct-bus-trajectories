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
import { deriveStat, cleanLabel, fetchSegFile } from "../network_data.js";

export const METRICS = {
  overall:          { label: "Overall delay (t_obs − t_ff)", kind: "delay", unit: "s" },
  pax:              { label: "Passenger-weighted delay", kind: "delay", unit: "pax·s" },
  nondwell:         { label: "Non-dwell delay",        kind: "delay", unit: "s" },
  dwell:            { label: "Dwell delay",            kind: "delay", unit: "s" },
  freeflow_speed:   { label: "Free flow speed",        kind: "static", unit: "mph" },
  avg_speed:        { label: "Average speed",          kind: "speed",  unit: "mph" },
  buses_per_hr:     { label: "Bus / hour",             kind: "rate", unit: "/hr" },
  boardings_per_hr: { label: "Boardings / hour",       kind: "rate", unit: "/hr" },
  // Headway CV at the downstream end minus the upstream end (build_cv
  // summary.json): red = bunching builds across the segment. Fixed to the
  // city's configured CV window, so it ignores the filters like free flow.
  cv_increase:      { label: "Headway CV increase",    kind: "static", unit: "cv" },
  // Median % speed change in heavy-rain (>= 7.6 mm/h, MRMS) or snow hours vs
  // the segment's usual normal-hour speed (weather_effect.py). Fixed window:
  // ignores the filters, like the other static metrics. Red = slower.
  heavy_rain_effect: { label: "Heavy rain speed effect", kind: "static", unit: "pct", cond: "heavy_rain" },
  snow_effect:       { label: "Snow speed effect",       kind: "static", unit: "pct", cond: "snow" },
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
  if (m.unit === "cv") return v.toFixed(2);
  if (m.unit === "pct") return `${v.toFixed(1)}%`;
  if (m.unit === "mph") return `${v.toFixed(0)}`;
  if (m.unit === "/hr") return v.toFixed(1);
  if (m.unit === "pax·s") {
    return Math.abs(v) >= 1000 ? `${(v / 1000).toFixed(1)}k` : `${v.toFixed(0)}`;
  }
  return `${v.toFixed(0)}s`;
}

// ---- smooth delay-profile line (2026-11-21) -------------------------------
// The 10 ft bars spread with a Gaussian weight (sigma LINE_SIGMA_FT) and
// evaluated every LINE_STEP_FT. Each bar's mass is renormalised inside its
// own zone, so the area under the line equals the area of the bars.
const LINE_SIGMA_FT = 20;
const LINE_STEP_FT = 5;
const LINE_PER_FT = 100;          // y-axis: quantity per 100 ft of street
function smoothFine(arr, bucketFt, lenFt) {
  const m = Math.ceil(lenFt / LINE_STEP_FT) + 1;
  const out = new Float64Array(m);
  const w = new Float64Array(m);
  arr.forEach((v, i) => {
    if (!v) return;
    const c = (i + 0.5) * bucketFt;
    const lo = Math.max(0, Math.floor((c - 3.2 * LINE_SIGMA_FT) / LINE_STEP_FT));
    const hi = Math.min(m - 1, Math.ceil((c + 3.2 * LINE_SIGMA_FT) / LINE_STEP_FT));
    let sum = 0;
    for (let j = lo; j <= hi; j++) {
      const z = (j * LINE_STEP_FT - c) / LINE_SIGMA_FT;
      w[j] = Math.exp(-z * z / 2);
      sum += w[j];
    }
    if (!sum) return;
    for (let j = lo; j <= hi; j++) out[j] += (v * w[j] / sum) * (bucketFt / LINE_STEP_FT);
  });
  return out;                      // per-bucket units, sampled every LINE_STEP_FT
}
// Sum of the ORIGINAL bars between two distances (fractional end buckets).
function integBuckets(arr, bucketFt, f0, f1) {
  const lo = Math.min(f0, f1), hi = Math.max(f0, f1);
  let t = 0;
  for (let i = Math.max(0, Math.floor(lo / bucketFt));
       i <= Math.min(arr.length - 1, Math.floor(hi / bucketFt)); i++) {
    const ov = Math.min(hi, (i + 1) * bucketFt) - Math.max(lo, i * bucketFt);
    if (ov > 0) t += (arr[i] * ov) / bucketFt;
  }
  return t;
}
// Whole-number ticks; decimals only when the maximum is below 1.
function lineTicks(max) {
  const out = [];
  if (max < 1) {
    const st = max < 0.4 ? 0.1 : max < 0.8 ? 0.2 : 0.5;
    for (let v = 0; v <= max + 1e-9; v += st) out.push(+v.toFixed(1));
    return out;
  }
  let st = 1;
  for (const c of [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000]) {
    st = c;
    if (max / c <= 4.5) break;
  }
  for (let v = 0; v <= max; v += st) out.push(v);
  return out;
}
const LINE_GROUP = { sp: "sp", nd: "nd", post_ns: "nd", post2_ns: "nd", pre: "pre",
                     post: "post", post2: "post", dw: "dw" };
const LINE_COL = { sp: "#2e3440", nd: "#d63a2f", pre: "#1fb8b0", post: "#8a4fc8", dw: "#2b6fd6" };
const LINE_NAME = { sp: "time spent", nd: "non-dwell", pre: "pre-boarding", post: "post-boarding",
                    dw: "door events" };

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
            <option value="avg_speed">Average speed</option>
            <option value="buses_per_hr">Bus / hour</option>
            <option value="boardings_per_hr" ${this.hasDoor ? "" : "disabled"}>Boardings / hour${this.hasDoor ? "" : " (needs door data)"}</option>
            <option value="cv_increase" ${this.data.cvSummary ? "" : "disabled"}>Headway CV increase${this.data.cvSummary ? "" : " (no headway-CV routes)"}</option>
          </optgroup>
          <optgroup label="Weather">
            <option value="heavy_rain_effect" ${this.data.weatherEffect ? "" : "disabled"}>Heavy rain speed effect${this.data.weatherEffect ? "" : " (no weather data)"}</option>
            <option value="snow_effect" ${this.data.weatherEffect ? "" : "disabled"}>Snow speed effect${this.data.weatherEffect ? "" : " (no weather data)"}</option>
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
      <div class="nw-group"><b>Weather</b>
        <div class="nw-inline">
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
      const order = dims.route_ids.map((_, i) => i).sort((a, b) =>
        this._routeLabel(dims.route_ids[a]).localeCompare(
          this._routeLabel(dims.route_ids[b]), undefined, { numeric: true }));
      order.forEach((i) => {
        const r = dims.route_ids[i];
        if (this._showSelectedOnly && !checked.has(i)) return;
        if (this._routeQuery && !r.toLowerCase().includes(this._routeQuery)
            && !this._routeLabel(r).toLowerCase().includes(this._routeQuery)) return;
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
        row.innerHTML = `<span>${this._routeLabel(r)}</span>${dirBtns}<input type="checkbox" ${checked.has(i) ? "checked" : ""}>`;
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
    for (const [id, key] of [["nw-weather", "weather"]]) {
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

  _routeLabel(rid) {
    const dims = this.data.meta.dims;
    this._rlabels ??= new Map(
      dims.route_ids.map((r, i) => [r, dims.route_labels?.[i] ?? r]));
    return this._rlabels.get(rid) ?? rid;
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
    if (fam === "overall" || fam === "buses_per_hr" || fam === "avg_speed") return acc.n;
    return acc.nDoor ?? 0;
  }

  async _valuesFor(filters) {
    const metric = this.N.metric;
    const values = new Map();
    this._tFf ??= new Map(
      this.data.segments.features.map((f) => [f.properties.sid, f.properties.t_ff_s]));
    const tFf = this._tFf;

    if (metric === "cv_increase") {
      for (const [sid, rec] of Object.entries(this.data.cvSummary?.segments ?? {})) {
        values.set(Number(sid), rec.d_cv);
      }
      return values;
    }
    if (METRICS[metric].cond) {
      const cond = METRICS[metric].cond;
      for (const [sid, rec] of Object.entries(this.data.weatherEffect?.segments ?? {})) {
        if (rec[cond]) values.set(Number(sid), rec[cond].pct);
      }
      return values;
    }
    if (metric === "freeflow_speed") {
      for (const f of this.data.segments.features) {
        const p = f.properties;
        if (p.t_ff_s > 0) values.set(p.sid, (p.len_m / p.t_ff_s) * 2.23694);
      }
      return values;
    }
    const combined = await this.data.combine(filters);
    if (metric === "avg_speed") {
      // observed speed: len / (t_ff + mean delay) — traversal-only, no door data
      this._lenM ??= new Map(
        this.data.segments.features.map((f) => [f.properties.sid, f.properties.len_m]));
      for (const [sid, acc] of combined) {
        if (this._minCount(acc) < this.N.minN) continue;
        const tObs = (tFf.get(sid) ?? 0) + acc.sum / acc.n;
        const len = this._lenM.get(sid);
        if (len > 0 && tObs > 0) values.set(sid, (len / tObs) * 2.23694);
      }
      return values;
    }
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

  // Spinner badge over the map while line colours load. A token so an
  // older refresh finishing late doesn't hide the badge of a newer one.
  _mapLoading(on) {
    let el = document.querySelector("#nw-map-loading");
    if (!el) {
      const host = $("map");
      if (!host) return;
      el = document.createElement("div");
      el.id = "nw-map-loading";
      el.className = "nw-loading-badge";
      el.innerHTML = `<span class="nw-spinner"></span> Loading…`;
      host.appendChild(el);
    }
    el.classList.toggle("hidden", !on);
  }

  async refresh() {
    this.N.syncHash?.();
    const cnt = document.querySelector("#nw-count");
    if (cnt) cnt.textContent = "loading data…";
    const token = (this._refreshToken = (this._refreshToken ?? 0) + 1);
    this._mapLoading(true);
    try {
      await this._refreshInner();
    } catch (err) {
      console.error("network refresh failed", err);
      if (cnt) cnt.textContent = `load failed: ${err.message || err}`;
    } finally {
      if (token === this._refreshToken) this._mapLoading(false);
    }
  }

  async _refreshInner() {
    const metric = this.N.metric;
    const spec = METRICS[metric];
    let visible = this._visibleSids();
    let values;
    let diverging = false;
    let legendTitle = spec.label + (spec.kind === "delay" ? ` · ${STATS[this.N.stat]}` : "");
    if (spec.cond) {
      // Diverging around 0; segments without enough weather hours stay grey.
      diverging = true;
      const m = this.data.weatherEffect?.meta;
      if (m) legendTitle += ` · median % vs usual for the hour · ≥${m.min_n} traversals over ≥${m.min_hours} hours (ignores filters)`;
    }
    if (metric === "cv_increase") {
      // Only segments with headway CV exist for this metric; + = more
      // bunching (red), − = headways even out (blue).
      const cvSids = new Set(Object.keys(this.data.cvSummary?.segments ?? {}).map(Number));
      visible = visible ? new Set([...visible].filter((s) => cvSids.has(s))) : cvSids;
      diverging = true;
      const w = this.data.cvSummary?.window;
      if (w) {
        const days = (w.daytypes ?? []).length === 4 ? "all days" : (w.daytypes ?? []).join("/");
        legendTitle += ` · ${days} ${w.hours_weekday?.[0]}:00–${w.hours_weekday?.[1]}:00, ${w.n_dates} dates (ignores filters)`;
      }
    }

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

  // context for the heavy-rain / snow metrics' hover
  _weatherHoverBit(sid) {
    const cond = METRICS[this.N.metric]?.cond;
    if (!cond) return "";
    const r = this.data.weatherEffect?.segments?.[sid]?.[cond];
    if (!r) return "<br>not enough traversals in these conditions";
    const sgn = (x) => (x > 0 ? "+" : "") + x.toFixed(1);
    const what = cond === "snow" ? "snow" : "heavy-rain";
    return `<br>${sgn(r.dmph)} mph (${r.cond_mph.toFixed(1)} vs usual ${r.base_mph.toFixed(1)})`
      + `<br>${r.n.toLocaleString()} traversals in ${r.hours} ${what} hours`;
  }

  // "route: CV up → down" lines for the CV-increase metric's hover.
  _cvHoverBit(sid) {
    if (this.N.metric !== "cv_increase") return "";
    const rec = this.data.cvSummary?.segments?.[sid];
    if (!rec) return "";
    return Object.entries(rec.routes).map(([r, x]) =>
      `<br>${this._routeLabel(r)}: CV ${x.cv_up.toFixed(2)} → ${x.cv_down.toFixed(2)}`).join("");
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
      // red = worse: for delay, + is worse; for speed, − (slower) is worse
      // negative speed / % change = slower = worse
      const worse = ["mph", "pct"].includes(METRICS[metric].unit) ? -1 : 1;
      const sc = d3.scaleSequential(d3.interpolateRdBu).domain([worse * m, -worse * m]);
      colorOf = (v) => sc(v);
      gradient = d3.range(0, 1.01, 0.1).map((t) => sc.interpolator()(1 - t));
    } else if (METRICS[metric].unit === "mph") {
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
    this._distThresh ??= "5";   // slow-event threshold pass: "5" | "3" mph
    const distDir = this._distThresh === "3" ? "dist3mph" : "dist";
    host.innerHTML = `<div class="nw-panel-loading"><span class="nw-spinner"></span> Loading delay locations…</div>`;
    let d;
    try {
      d = await fetchSegFile(this.data.base, distDir, props.sid);
      if (!d) throw new Error();
      // headway CV (frequent network only): absent file just greys the box
      if (this._cvSeg !== props.sid) {
        this._cvSeg = props.sid;
        this._cv = null;
        try {
          this._cv = await fetchSegFile(this.data.base, "cv", props.sid);
        } catch { /* keep null */ }
        // heavy-rain / snow per-10 ft speeds (weather_speed_profile.py)
        this._wx = null;
        if (this.data.weatherEffect) {
          try {
            this._wx = await fetchSegFile(this.data.base, "wx_speed", props.sid);
          } catch { /* keep null */ }
        }
      }
    } catch {
      if (this._distThresh === "3") {
        host.innerHTML = `<div class="nw-note">no 3 mph pass built for this city/segment —
          <a href="#" class="dist-back5">back to 5 mph</a></div>`;
        host.querySelector(".dist-back5").onclick = (e) => {
          e.preventDefault();
          this._distThresh = "5";
          this._renderDistribution(host, props, coords);
        };
        return;
      }
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
    this._distMode ??= "seconds"; // "seconds" | "pax" | "events" | "queue"
    if (this._distMode === "pings") this._distMode = "seconds"; // tab removed 2026-08-25
    if (this._distMode === "pax" && !d.nd_p) this._distMode = "seconds";
    if (this._distMode === "spent" && !d.ping_v) this._distMode = "seconds";
    const pingMode = this._distMode === "pings";
    // "time spent": ALL seconds a bus spends in each 10 ft, not only located
    // delays. No new data: the avg-speed overlay is bucket length / mean
    // crossing time (build_distributions), so time = bucket length / speed.
    const spentMode = this._distMode === "spent";
    const secondsMode = this._distMode === "seconds";
    const paxMode = this._distMode === "pax";
    const queueMode = this._distMode === "queue";
    const suffix = secondsMode ? "_s" : paxMode ? "_p" : queueMode ? "_q" : "";
    // v2 classes; v1 files simply lack post2/dw (empty fallbacks keep them
    // working). dw (door events) is opt-in via the checkbox.
    this._showDoors ??= false;
    // Stack order groups every red-rendered class together (nd + the *_ns
    // classes, currently drawn nd-red) so they read as ONE red bar, with
    // teal/purple above.
    const CLASSES = pingMode ? ["ping"] : spentMode ? ["sp"]
                   : ["nd", "post_ns", "post2_ns", "pre", "post", "post2",
                      ...(this._showDoors ? ["dw"] : [])];
    // Turn-movement filter (T/L/R/E through the downstream signal).
    // Selection resets when the popup moves to a different segment.
    const mvmtAll = d.mvmt ? Object.keys(d.mvmt) : [];
    if (this._mvmtSeg !== props.sid) {
      this._mvmtSeg = props.sid;
      this._mvmtSel = new Set(mvmtAll);
    }
    const mvmtFiltered = !pingMode && !spentMode && d.by_mvmt && this._mvmtSel.size < mvmtAll.length;
    // Seconds mode: divide by the traversal count so the y-axis reads as
    // AVERAGE delay seconds per trip (dumb rescale, per user). When the
    // movement filter is active, both the arrays and the denominator come
    // from the selected movements only.
    const selTrips = mvmtFiltered
      ? [...this._mvmtSel].reduce((a, m) => a + (d.mvmt[m] ?? 0), 0)
      : (d.n_trips ?? 1);
    const denom = (secondsMode || paxMode) ? Math.max(1, selTrips) : 1;
    const zeros = d.nd.map(() => 0);
    const BUCKET_M = d.bucket_ft / 3.28084;
    const ffPerBucket = (props.t_ff_s > 0 && props.len_m > 0)
      ? (props.t_ff_s * BUCKET_M) / props.len_m : 0;   // free-flow seconds per 10 ft
    // seconds per trip in each bucket from its stored speed; the last bucket
    // is shorter than 10 ft, buckets without a speed fall back to free-flow
    const spentOf = (vArr, n, lastShort) => Array.from({ length: n }, (_, i) => {
      const v = vArr?.[i];
      const len = lastShort && i === n - 1
        ? Math.max(0.01, props.len_m - (n - 1) * BUCKET_M) : BUCKET_M;
      return v > 0 ? len / v : (ffPerBucket * len) / BUCKET_M;
    });
    const pick = (c) => {
      if (c === "sp") return spentOf(d.ping_v, zeros.length, true);
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
    const W = 990, chartH = 270, roadH = 64, axisH = 30, padL = 28, padR = 58;
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
    // Mirrored 2026-08-16: signal (0 ft) on the RIGHT, travel rightward.
    const xOf = (ft) => W - padR - ((ft + ghFt) / domFt) * innerW;
    const bw = Math.max(1, (d.bucket_ft / domFt) * innerW);

    const totals = src[CLASSES[0]].map((_, i) =>
      CLASSES.reduce((a, c) => a + src[c][i], 0));
    const yMax = Math.max(1, ...totals);
    const yOf = (n) => chartH - (n / yMax) * (chartH - 6);

    // *_ns = post at a NEAR-SIDE stop: a stop-then-signal compound, drawn
    // as purple over red rather than either alone.
    const COLORS = { nd: "#d63a2f", pre: "#1fb8b0",
                     post: "#8a4fc8", post2: "url(#post2hatch)",
                     post_ns: "#d63a2f", post2_ns: "#d63a2f",   // plain red for now
                     dw: "#2b6fd6", ping: "#3f3f3f", sp: "#3f3f3f" };
    let bars = `<defs><pattern id="post2hatch" width="6" height="6"
        patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
        <rect width="6" height="6" fill="#8a4fc8"/>
        <line x1="0" y1="0" x2="0" y2="6" stroke="#fff" stroke-width="2.2"/>
      </pattern>
      <pattern id="nshatch" width="7" height="7"
        patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
        <rect width="7" height="7" fill="#8a4fc8"/>
        <rect width="3.4" height="7" fill="#d63a2f"/>
      </pattern>
      <pattern id="ns2hatch" width="7" height="7"
        patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
        <rect width="7" height="7" fill="#8a4fc8"/>
        <rect width="3.4" height="7" fill="#d63a2f"/>
        <line x1="0" y1="0" x2="0" y2="7" stroke="#fff" stroke-width="1.6"/>
      </pattern></defs>`;
    for (let i = 0; i < nB; i++) {
      let y = chartH;
      for (const cls of CLASSES) {
        const v = src[cls][i];
        if (!v) continue;
        const h = ((v / yMax) * (chartH - 6));
        y -= h;
        bars += `<rect x="${xOf((i + 1) * d.bucket_ft).toFixed(1)}" y="${y.toFixed(1)}"
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
            ghost += `<rect x="${xOf(base + (i + 1) * d.bucket_ft).toFixed(1)}" y="${y.toFixed(1)}"
                     width="${bw.toFixed(1)}" height="${h.toFixed(1)}" fill="${COLORS[cls]}"/>`;
          }
        }
      }
      if (ghost) bars += `<g opacity="0.5">${ghost}</g>`;
    }

    // y axis: 0, mid, max (+ unit label so the two axes read apart)
    const fmtY = (v) => secondsMode
      ? (v >= 60 ? `${(v / 60).toFixed(1)}m` : `${v.toFixed(1)}s`)
      : String(Math.round(v));
    const countUnit = pingMode ? "pings"
      : secondsMode ? "s/trip" : paxMode ? "pax·s/trip"
      : queueMode ? "events" : "events";
    const crx = W - padR + 4;
    let yAxis = `
      <line x1="${crx}" y1="8" x2="${crx}" y2="${chartH}" stroke="#999"/>
      <text x="${crx + 4}" y="16" class="dist-tick">${fmtY(yMax)}</text>
      <text x="${crx + 4}" y="${(chartH + 16) / 2}" class="dist-tick">${fmtY(yMax / 2)}</text>
      <text x="${crx + 4}" y="${chartH}" class="dist-tick">0</text>
      <text x="${crx + 4}" y="6" class="dist-tick" style="font-style:italic">${countUnit}</text>`;

    // ---- line style (default): smooth line in place of the bars ----------
    this._distStyle ??= "line";    // "line" | "bars"
    const lineMode = this._distStyle === "line" && !pingMode;
    const qtyUnit = (secondsMode || spentMode) ? "s per trip" : paxMode ? "pax·s per trip" : "events";
    const fmtQ = (v) => (v >= 10 ? Math.round(v).toLocaleString() : v.toFixed(1));
    let LN = null;                 // geometry the drag-to-measure handlers need
    // delay types as drawn (red / teal / purple / blue), per 10 ft bar:
    // what the segment total and drag-to-measure sum, in either style
    const groups = pingMode ? [] : [...new Set(CLASSES.map((c) => LINE_GROUP[c]))];
    const grpBars = Object.fromEntries(groups.map((g) => [g, zeros.slice()]));
    if (!pingMode)
      for (const c of CLASSES) src[c].forEach((v, i) => { grpBars[LINE_GROUP[c]][i] += v; });
    const total = groups.reduce((a, g) => a + grpBars[g].reduce((x, y) => x + y, 0), 0);
    const ffSeg = spentMode && props.t_ff_s > 0 ? props.t_ff_s : 0;
    if (lineMode) {
      const bf = d.bucket_ft, k = LINE_PER_FT / bf, step = LINE_STEP_FT;
      // Smooth over the segment AND its neighbour zones as one series, so the
      // line runs continuously through each end (smoothing the three pieces
      // separately left a step at both boundaries, 2026-11-21). Delay within
      // ~2 sigma of an end therefore blends with the neighbour's; totals and
      // drag measurements still come from the segment's own bars.
      const nB0 = zeros.length, o = (ghB * bf) / step;
      const ghGrp = (side) => {
        const out = Object.fromEntries(groups.map((g) => [g, new Array(ghB).fill(0)]));
        if (spentMode) {
          // ping_v_lo[0] is the bucket nearest the light; gh_lo runs the other way
          const v = side === "gh_lo" ? [...(d.ping_v_lo ?? [])].reverse() : (d.ping_v_hi ?? []);
          out.sp = spentOf(v, ghB, false);
          return out;
        }
        for (const c of CLASSES) {
          const key = c + suffix, tgt = out[LINE_GROUP[c]];
          if (mvmtFiltered) {
            for (const mv of this._mvmtSel) {
              const a = d.by_mvmt[mv]?.[side]?.[key] ?? d.by_mvmt[mv]?.[side]?.[c];
              if (a) a.forEach((v, i) => { tgt[i] += v / denom; });
            }
          } else {
            const a = d[side]?.[key] ?? d[side]?.[c];
            if (a) a.forEach((v, i) => { tgt[i] += v / denom; });
          }
        }
        return out;
      };
      const gLo = ghB ? ghGrp("gh_lo") : null, gHi = ghB ? ghGrp("gh_hi") : null;
      const E = Object.fromEntries(groups.map((g) => [g, smoothFine(
        ghB ? [...gLo[g], ...grpBars[g], ...gHi[g]] : grpBars[g], bf,
        ghB ? (2 * ghB + nB0) * bf : lenFt)]));
      const m = Math.ceil(lenFt / step) + 1;
      const F = Object.fromEntries(groups.map((g) => [g, E[g].subarray(o, o + m)]));
      const T = new Float64Array(m);
      for (const g of groups) for (let j = 0; j < m; j++) T[j] += F[g][j];
      // neighbour zones: [ft of first sample, total line] for each side
      const ghost = [];
      if (ghB) {
        const nE = E[groups[0]].length;
        const tot = (j) => groups.reduce((a2, g) => a2 + E[g][j], 0);
        const lo = [], hi = [];
        for (let j = 0; j <= o; j++) lo.push([j * step - ghFt, tot(j)]);
        for (let j = o + m - 1; j < nE; j++) hi.push([Math.min(lenFt + ghFt, j * step - ghB * bf), tot(j)]);
        lo[lo.length - 1][0] = 0;
        hi[0][0] = lenFt;
        ghost.push(lo, hi);
      }
      // the original 10 ft bars stay as a faint trace behind the line, so
      // the axis has to reach the tallest bar, not only the line's peak
      const barTot = zeros.map((_, i) => groups.reduce((a, g) => a + grpBars[g][i], 0));
      let peak = Math.max(0, ffPerBucket && spentMode ? ffPerBucket * 1.5 : 0, ...barTot);
      for (let j = 0; j < m; j++) peak = Math.max(peak, T[j]);
      peak *= k;
      const tk = lineTicks(peak || 1);
      const yMaxU = Math.max(peak, tk[tk.length - 1]) * 1.06;
      const yU = (u) => chartH - (u / yMaxU) * (chartH - 6);
      const yL = (v) => yU(v * k);
      const pts = [];
      for (let j = 0; j < m; j++) pts.push([xOf(Math.min(lenFt, j * step)), yL(T[j])]);
      const dom = (j) => {
        let b = groups[0], bv = -1;
        for (const g of groups) if (F[g][j] > bv) { bv = F[g][j]; b = g; }
        return b;
      };
      const P = (a) => a.map((q) => `${q[0].toFixed(1)},${q[1].toFixed(1)}`).join("L");
      let line = "";
      for (const v of tk)
        line += `<line x1="${padL}" x2="${W - padR}" y1="${yU(v).toFixed(1)}"
                 y2="${yU(v).toFixed(1)}" stroke="#eceef1"/>`;
      for (const G of ghost) {
        const gp = G.map(([ft, v]) => [xOf(ft), Math.max(8, yL(v))]);   // clipped at the top
        if (gp.length > 1) line += `<path d="M${P(gp)}" fill="none" stroke="#888"
          stroke-width="1.6" stroke-dasharray="4 3" opacity="0.6"/>`;
      }
      barTot.forEach((v, i) => {
        if (v > 0) line += `<rect x="${xOf((i + 1) * bf).toFixed(1)}" y="${yL(v).toFixed(1)}"
          width="${Math.max(0.6, bw - 0.4).toFixed(1)}" height="${(chartH - yL(v)).toFixed(1)}"
          fill="#3c4650" fill-opacity="0.16"/>`;
      });
      line += `<g class="dist-sel"></g>`;
      for (let j0 = 0, j = 1; j <= m; j++) {
        if (j === m || dom(j) !== dom(j0)) {
          const run = pts.slice(Math.max(0, j0 - 1), j);
          if (run.length > 1) line += `<path d="M${P(run)}" fill="none"
            stroke="${LINE_COL[dom(j0)]}" stroke-width="2.6" stroke-linejoin="round"
            stroke-linecap="round"/>`;
          j0 = j;
        }
      }
      if (spentMode && ffPerBucket) {
        // free-flow floor: what a bus running at the segment's free-flow
        // speed spends in every 10 ft; the gap above it is time lost
        const yf = yL(ffPerBucket).toFixed(1);
        line += `<line x1="${padL}" x2="${W - padR}" y1="${yf}" y2="${yf}" stroke="#2e8b57"
                  stroke-width="1.4" stroke-dasharray="6 4"/>
                 <text x="${padL + 4}" y="${(+yf - 4).toFixed(1)}" font-size="10" fill="#2e8b57"
                  style="paint-order:stroke;stroke:#fff;stroke-width:3px">free-flow</text>`;
      }
      bars = line;
      yAxis = `<line x1="${crx}" y1="8" x2="${crx}" y2="${chartH}" stroke="#999"/>` +
        tk.map((v) => `<text x="${crx + 4}" y="${(yU(v) + 4).toFixed(1)}"
          class="dist-tick">${v}</text>`).join("") +
        `<text x="${W - 2}" y="-2" text-anchor="end" class="dist-tick"
          style="font-style:italic">${(secondsMode || spentMode) ? "s/trip" : paxMode ? "pax·s/trip" : "events"} per ${LINE_PER_FT} ft</text>`;
      LN = { bf, step, m, T, pts, dom, groups, grpBars, yL, total, ff: ffSeg };
    } else if (!pingMode) {
      bars += `<g class="dist-sel"></g>`;      // drag-to-measure overlay, above the bars
    }
    // SX: what the total and drag-to-measure read, for the line or the bars
    const SX = LN ?? (pingMode ? null
      : { bf: d.bucket_ft, groups, grpBars, total, ff: ffSeg });

    // Average-speed overlay (raw ping speeds per 10 ft bucket): join-the-
    // dots line + right-hand mph axis, opposite end from the traffic light.
    this._showSpeed ??= false;
    const MPH = 2.23694;
    const spd = (this._showSpeed && d.ping_v) ? d.ping_v : null;
    // weather sub-overlays: only offered (and drawn) while avg speed is on
    this._showWxRain ??= false;
    this._showWxSnow ??= false;
    const wxRain = (spd && this._showWxRain) ? this._wx?.heavy_rain?.v : null;
    const wxSnow = (spd && this._showWxSnow) ? this._wx?.snow?.v : null;
    let vMaxMph = 0, ySpd = null, speedLine = "";
    if (spd) {
      const allV = [...spd, ...(wxRain ?? []), ...(wxSnow ?? [])].filter((v) => v != null);
      vMaxMph = Math.max(...allV) * MPH * 1.15 || 1;
      ySpd = (mph) => chartH - (mph / vMaxMph) * (chartH - 6);
      const mkPts = (arr, ftOf) => {
        const out = [];
        (arr ?? []).forEach((v, i) => {
          if (v != null) out.push([xOf(ftOf(i)), ySpd(v * MPH)]);
        });
        return out;
      };
      const pts = mkPts(spd, (i) => (i + 0.5) * d.bucket_ft);
      const drawLine = (pp, op) => pp.length > 1
        ? `<g opacity="${op}"><polyline points="${pp.map((p2) => p2.map((c) => c.toFixed(1)).join(",")).join(" ")}"
             fill="none" stroke="#0a0a0a" stroke-width="1.6"/>` +
          pp.map((p2) => `<circle cx="${p2[0].toFixed(1)}" cy="${p2[1].toFixed(1)}" r="1.6" fill="#0a0a0a"/>`).join("") + "</g>"
        : "";
      speedLine = drawLine(pts, 0.85);
      const wxLine = (arr, color) => {
        const pp = mkPts(arr, (i) => (i + 0.5) * d.bucket_ft);
        return pp.length > 1
          ? `<polyline points="${pp.map((p2) => p2.map((c) => c.toFixed(1)).join(",")).join(" ")}"
               fill="none" stroke="${color}" stroke-width="1.6" opacity="0.9"/>`
          : "";
      };
      if (wxRain) speedLine += wxLine(wxRain, "#d62728");
      if (wxSnow) speedLine += wxLine(wxSnow, "#2b6cb0");
      if (ghB) {
        // buffer extensions: neighbors' speeds at ghost opacity, stitched
        // to the main line's ends so the curve reads continuously
        const lo = mkPts(d.ping_v_lo, (i) => -(i + 0.5) * d.bucket_ft);
        const hi = mkPts(d.ping_v_hi, (i) => lenFt + (i + 0.5) * d.bucket_ft);
        if (lo.length && pts.length) lo.unshift(pts[0]);
        if (hi.length && pts.length) hi.unshift(pts[pts.length - 1]);
        speedLine += drawLine(lo, 0.42) + drawLine(hi, 0.42);
      }
      yAxis += `
        <line x1="${padL - 4}" y1="8" x2="${padL - 4}" y2="${chartH}" stroke="#999"/>
        <text x="${padL - 8}" y="16" text-anchor="end" class="dist-tick">${vMaxMph.toFixed(0)}</text>
        <text x="${padL - 8}" y="${(chartH + 16) / 2}" text-anchor="end" class="dist-tick">${(vMaxMph / 2).toFixed(0)}</text>
        <text x="${padL - 8}" y="${chartH}" text-anchor="end" class="dist-tick">0</text>
        <text x="${padL - 8}" y="6" text-anchor="end" class="dist-tick" style="font-style:italic">mph</text>`;
    }

    // headway CV overlay: one line per frequent route through the segment.
    // Dimensionless, own scale: a fixed 0.05-wide window centered on the
    // segment's CV range (widened only if the data spans more), so small
    // within-segment changes stay visible. The dashed rule marks CV = 0.5,
    // a common bunching threshold, when it falls inside the window.
    let cvLines = "";
    const cvOn = this._showCV && this._cv;
    if (cvOn) {
      const CV_COLORS = ["#b8860b", "#7b3fa0", "#00838f", "#c2185b", "#5d4037"];
      const routes = Object.keys(this._cv.routes);
      let cvLo = Infinity, cvHi = -Infinity;
      for (const r of routes)
        for (const v of this._cv.routes[r].cv) if (v != null) {
          cvLo = Math.min(cvLo, v); cvHi = Math.max(cvHi, v);
        }
      if (!isFinite(cvLo)) { cvLo = 0; cvHi = 1; }
      const cvMid = (cvLo + cvHi) / 2;
      const cvSpan = Math.max(0.05, (cvHi - cvLo) * 1.08);
      const winLo = cvMid - cvSpan / 2, winHi = cvMid + cvSpan / 2;
      const yCv = (v) => 6 + (1 - (v - winLo) / cvSpan) * (chartH - 6);
      if (winLo <= 0.5 && 0.5 <= winHi)
        cvLines = `<line x1="${padL}" y1="${yCv(0.5).toFixed(1)}" x2="${W - padR}" y2="${yCv(0.5).toFixed(1)}"
          stroke="#b8860b" stroke-width="0.7" stroke-dasharray="5,4" opacity="0.5"/>`;
      routes.forEach((r, ri) => {
        const rec = this._cv.routes[r];
        const col = CV_COLORS[ri % CV_COLORS.length];
        const pts = [];
        rec.cv.forEach((v, i) => {
          if (v != null) pts.push([xOf((i + 0.5) * this._cv.bucket_ft), yCv(v)]);
        });
        if (pts.length > 1) {
          cvLines += `<polyline points="${pts.map((q) => q.map((c) => c.toFixed(1)).join(",")).join(" ")}"
            fill="none" stroke="${col}" stroke-width="1.8" opacity="0.9"/>`;
          const [lx, ly] = pts[pts.length - 1];
          cvLines += `<text x="${(lx - 4).toFixed(1)}" y="${(ly - 5).toFixed(1)}" font-size="10"
            fill="${col}" text-anchor="end">rt ${r}</text>`;
        }
      });
      yAxis += `
        <text x="${padL + 4}" y="16" class="dist-tick" fill="#b8860b">CV ${winHi.toFixed(3)}</text>
        <text x="${padL + 4}" y="${(chartH - 4).toFixed(1)}" class="dist-tick" fill="#b8860b">CV ${winLo.toFixed(3)}</text>`;
      if (winLo <= 0.5 && 0.5 <= winHi)
        yAxis += `
        <text x="${padL + 4}" y="${yCv(0.5) - 3}" class="dist-tick" fill="#b8860b" opacity="0.7">0.5</text>`;
    }

    // road strip
    const roadY = chartH + 8;
    const roadBodyH = roadH - 18;
    const axisY = roadY + roadBodyH + 52;
    // Break the roadway (12 px gaps) at NAMED junctions — non-signalized
    // by definition (signals are segment boundaries); label each gap.
    const namedJcts = (props.junctions_off ?? [])
      .filter((j) => j.cross)
      .map((j) => ({ x: xOf(j.off_m * 3.28084), cross: j.cross }))
      .filter((j) => j.x > xOf(lenFt) + 18 && j.x < xOf(0) - 18)
      .sort((a, b) => a.x - b.x);
    let road = "";
    {
      const cy = roadY + roadBodyH / 2;
      let cursor = xOf(lenFt);
      const spans = [];
      for (const j of namedJcts) {
        spans.push([cursor, j.x - 6]);
        cursor = j.x + 6;
      }
      spans.push([cursor, xOf(0)]);
      for (const [a, b] of spans) {
        if (b - a < 2) continue;
        road += `<rect x="${a.toFixed(1)}" y="${roadY}" width="${(b - a).toFixed(1)}"
                 height="${roadBodyH}" rx="6" fill="#4a4a52"/>
          <line x1="${(a + 4).toFixed(1)}" y1="${cy}" x2="${(b - 4).toFixed(1)}" y2="${cy}"
                stroke="#fff" stroke-width="2.5" stroke-dasharray="16 13" opacity=".7"/>`;
      }
      for (const j of namedJcts) {
        const nm = j.cross.replace(/^(North|South|East|West) /, "");
        road += `<text transform="rotate(-90 ${j.x.toFixed(1)} ${roadY})" x="${j.x.toFixed(1)}"
                 y="${roadY}" text-anchor="end"
                 style="font-size:8px;fill:#555" dominant-baseline="middle">${nm}</text>`;
      }
    }
    // travel direction: a few left-pointing chevrons on the centerline
    {
      const cy = roadY + roadBodyH / 2;
      for (const frac of [0.22, 0.5, 0.78]) {
        const ax = xOf(lenFt * frac);
        road += `<path d="M ${(ax - 7).toFixed(1)} ${cy - 6} L ${(ax + 5).toFixed(1)} ${cy}
                 L ${(ax - 7).toFixed(1)} ${cy + 6}" fill="none" stroke="#fff"
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
    road += `<g transform="translate(${(xOf(0) + 15).toFixed(1)}, ${ly - 22})">
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
        `<text transform="rotate(-90 ${tx} ${roadY})" x="${tx}" y="${roadY}"
         text-anchor="end" style="font-size:8px;fill:${fill}">${name}</text>`;
      if (downName) road += sideLabel((xOf(0) + 46).toFixed(1), downName);
      if (upName) road += sideLabel((xOf(lenFt) - 14).toFixed(1), upName);
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
          <text x="${x.toFixed(1)}" y="${top + bh + 13}"
                text-anchor="middle" style="font-size:9.5px;fill:#333;font-weight:600">${st.id}</text>
          <text x="${x.toFixed(1)}" y="${top + bh + 24}"
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
        <text class="dist-cursor-spd" style="font-size:10px;font-weight:700;
              paint-order:stroke;stroke:#fff;stroke-width:3px;fill:#0a0a0a"></text>
      </g>`;

    host.innerHTML = `
      <hr class="dist-rule">
      <div class="dist-head">${spentMode ? "Time spent along the segment" : this.hasDoor
          ? "Distribution of non-boarding delays"
          : "Distribution of delay locations"}
        <span class="dist-toggle">
          <button data-m="seconds" class="${this._distMode === "seconds" ? "on" : ""}">time</button>
          ${d.nd_p ? `<button data-m="pax" class="${this._distMode === "pax" ? "on" : ""}">pax</button>` : ""}
          <button data-m="events" class="${this._distMode === "events" ? "on" : ""}">events</button>
          ${d.nd_q ? `<button data-m="queue" class="${this._distMode === "queue" ? "on" : ""}">last stop</button>` : ""}
          ${d.ping_v ? `<button data-m="spent" class="${spentMode ? "on" : ""}"
            title="all seconds a bus spends in each stretch, not only located delays">time spent</button>` : ""}
        </span>
        ${d.ping_v ? `<label class="dist-speed" style="font-size:11px;margin-left:10px;cursor:pointer">
          <input type="checkbox" class="dist-speed-cb" ${this._showSpeed ? "checked" : ""}> avg speed</label>` : ""}
        ${d.ping_v && this._showSpeed && this._wx?.heavy_rain ? `<label class="dist-wx" style="font-size:11px;margin-left:8px;cursor:pointer;color:#d62728"
          title="average speed in hours with >= 7.6 mm of rain at this segment">
          <input type="checkbox" class="dist-wx-rain-cb" ${this._showWxRain ? "checked" : ""}> heavy rain</label>` : ""}
        ${d.ping_v && this._showSpeed && this._wx?.snow ? `<label class="dist-wx" style="font-size:11px;margin-left:8px;cursor:pointer;color:#2b6cb0"
          title="average speed in hours with falling snow at the city's airport">
          <input type="checkbox" class="dist-wx-snow-cb" ${this._showWxSnow ? "checked" : ""}> snow</label>` : ""}
        <label class="dist-cv" style="font-size:11px;margin-left:10px;cursor:pointer;${this._cv ? "" : "opacity:.4"}"
          title="${this._cv ? "coefficient of variation of headways (routes configured per city)" : "no headway-CV route through this segment in this city"}">
          <input type="checkbox" class="dist-cv-cb" ${this._cv ? "" : "disabled"} ${this._showCV && this._cv ? "checked" : ""}> headway CV</label>
        ${this.hasDoor && d.dw && !pingMode && !spentMode ? `<label class="dist-doors" style="font-size:11px;margin-left:10px;cursor:pointer">
          <input type="checkbox" class="dist-doors-cb" ${this._showDoors ? "checked" : ""}> door events</label>` : ""}
        ${mvmtAll.length && !pingMode && !spentMode ? `<span class="dist-mvmt" style="margin-left:10px;display:inline-flex;gap:3px;vertical-align:middle">
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
        ${SX ? `<br><span class="dist-total"><b>${fmtQ(SX.total)}</b> ${qtyUnit} ${spentMode ? "average travel time" : "on this segment"}${
          SX.ff ? ` · ${fmtQ(SX.ff)} s at free-flow · ${fmtQ(Math.max(0, SX.total - SX.ff))} s above it` : ""}</span>` : ""}
        <span class="nw-note">${pingMode
          ? `(${totals.reduce((a, v) => a + v, 0).toLocaleString()} raw AVL pings, pre-reconstruction)`
          : `(${d.n_events} events · ${d.n_trips ?? "?"} trips)`}</span>
      </div>
      <div class="dist-wrap"><svg viewBox="0 -12 ${W} ${H + 12}" class="dist-svg${SX ? " dist-line" : ""}">${yAxis}${bars}${speedLine}${cvLines}${road}${axis}</svg>
        <div class="dist-call hidden"></div></div>
      ${SX ? `<div class="dist-hint"><span>Drag across the chart to measure the ${spentMode ? "time spent" : "delay"} between two points.
        <a href="#" class="dist-sel-clear hidden">clear</a></span>
        <span class="dist-style" title="smooth line, or the original 10 ft bars">
          <button data-st="line" class="${lineMode ? "on" : ""}">line</button><button
            data-st="bars" class="${lineMode ? "" : "on"}">10 ft bars</button></span></div>` : ""}
      ${this.hasDoor && !pingMode && !spentMode ? `<div class="dist-legend">
        <span><i style="background:#d63a2f"></i>non-dwell</span>
        <span><i style="background:#1fb8b0"></i>pre-boarding</span>
        <span><i style="background:#8a4fc8"></i>post-boarding</span>
        <span><i style="background:repeating-linear-gradient(45deg,#8a4fc8,#8a4fc8 3px,#fff 3px,#fff 5px)"></i>post-boarding, extra door cycles</span>
        ${this._showDoors ? `<span><i style="background:#2b6fd6"></i>door events</span>` : ""}
      </div>` : ""}`;
    host.querySelectorAll(".dist-toggle button").forEach((b) => {
      b.onclick = () => {
        if (b.dataset.th) this._distThresh = b.dataset.th;
        else this._distMode = b.dataset.m;
        this._renderDistribution(host, props, coords);
      };
    });
    host.querySelectorAll(".dist-style button").forEach((b) => {
      b.onclick = () => {
        this._distStyle = b.dataset.st;
        this._renderDistribution(host, props, coords);
      };
    });
    // Drag to measure (line style): shade the area under the curve between
    // two points and float the integrated quantity next to it. Figures are
    // summed from the original bars, so they do not depend on the smoothing.
    if (SX) {
      const svgL = host.querySelector(".dist-svg");
      const selG = svgL.querySelector(".dist-sel");
      const call = host.querySelector(".dist-call");
      const clearA = host.querySelector(".dist-sel-clear");
      if (this._distSel?.sid !== props.sid) this._distSel = null;
      const ftAt = (e) => {
        const rect = svgL.getBoundingClientRect();
        const px = (e.clientX - rect.left) * (W / rect.width);
        return ((W - padR - px) / innerW) * domFt - ghFt;
      };
      const clampFt = (f) => Math.max(0, Math.min(lenFt, f));
      const at = (f) => {       // line height at a distance (line style only)
        const u = Math.min(LN.m - 1.001, Math.max(0, f / LN.step)), i = Math.floor(u);
        return LN.T[i] + (LN.T[i + 1] - LN.T[i]) * (u - i);
      };
      const drawSel = () => {
        const sel = this._distSel;
        if (!sel || Math.abs(sel.a - sel.b) < 5) {
          selG.innerHTML = "";
          call.classList.add("hidden");
          clearA?.classList.add("hidden");
          return;
        }
        const fa = Math.max(sel.a, sel.b), fb = Math.min(sel.a, sel.b);
        let g = "", yTop = chartH;
        const guides = (yAt) => {
          for (const f of [fa, fb]) {
            const x = xOf(f).toFixed(1), y = yAt(f).toFixed(1);
            g += `<line x1="${x}" x2="${x}" y1="8" y2="${y}" stroke="#777" stroke-opacity=".6"
                   stroke-dasharray="3 4"/>
                  <line x1="${x}" x2="${x}" y1="${y}" y2="${chartH}" stroke="#1c1c28"
                   stroke-width="1.5"/>`;
          }
        };
        if (LN) {
          // line: shade the area under the curve in the line's colour
          const ja = Math.min(LN.m - 1, Math.floor(fa / LN.step));
          const jb = Math.max(0, Math.ceil(fb / LN.step));
          const jn = (f) => Math.max(0, Math.min(LN.m - 1, Math.round(f / LN.step)));
          const Q = [[xOf(fa), LN.yL(at(fa)), LN.dom(jn(fa))]];
          for (let j = ja; j >= jb; j--) Q.push([LN.pts[j][0], LN.pts[j][1], LN.dom(j)]);
          Q.push([xOf(fb), LN.yL(at(fb)), LN.dom(jn(fb))]);
          for (const q of Q) yTop = Math.min(yTop, q[1]);
          for (let i0 = 0, i = 1; i <= Q.length; i++) {
            if (i === Q.length || Q[i][2] !== Q[i0][2]) {
              const run = Q.slice(i0, Math.min(Q.length, i + 1));
              if (run.length > 1) g += `<path d="M${run[0][0].toFixed(1)},${chartH}L${
                run.map((q) => `${q[0].toFixed(1)},${q[1].toFixed(1)}`).join("L")}L${
                run[run.length - 1][0].toFixed(1)},${chartH}Z"
                fill="${LINE_COL[Q[i0][2]]}" fill-opacity="0.5"/>`;
              i0 = i;
            }
          }
          guides((f) => LN.yL(at(f)));
        } else {
          // bars: the selected bars keep their colours, the rest of the
          // segment is veiled
          const nB1 = totals.length;
          const bi = (f) => Math.max(0, Math.min(nB1 - 1, Math.floor(f / SX.bf)));
          for (let i = bi(fb); i <= bi(fa); i++) yTop = Math.min(yTop, yOf(totals[i]));
          const veil = (f0, f1) => {
            const x0 = xOf(f0), x1 = xOf(f1);
            if (x1 - x0 > 0.5) g += `<rect x="${x0.toFixed(1)}" y="6" width="${(x1 - x0).toFixed(1)}"
              height="${chartH - 6}" fill="#fff" fill-opacity="0.66"/>`;
          };
          veil(lenFt, fa);
          veil(fb, 0);
          guides((f) => yOf(totals[bi(f)]));
        }
        selG.innerHTML = g;
        const parts = SX.groups
          .map((gk) => [gk, integBuckets(SX.grpBars[gk], SX.bf, fa, fb)]);
        const t = parts.reduce((a, q) => a + q[1], 0);
        const ffSel = SX.ff ? (SX.ff * (fa - fb)) / lenFt : 0;
        call.innerHTML = `<div><b>${fmtQ(t)}</b> ${qtyUnit}</div>
          ${SX.ff ? `<div class="r">${fmtQ(Math.max(0, t - ffSel))} s above free-flow</div>` : ""}
          <div class="r">${Math.round(fa / 10) * 10} to ${Math.round(fb / 10) * 10} ft before the light</div>
          ${parts.length > 1 ? `<div class="parts">${parts.filter((q) => q[1] >= 0.05)
            .map(([gk, v]) => `<span><i style="background:${LINE_COL[gk]}"></i>${
              LINE_NAME[gk]} ${fmtQ(v)}</span>`).join("")}</div>` : ""}`;
        call.classList.remove("hidden");
        clearA?.classList.remove("hidden");
        // attach the box to the area: above its highest point if it fits,
        // otherwise beside it
        const rect = svgL.getBoundingClientRect();
        const sc = rect.width / W, cw = call.offsetWidth, ch = call.offsetHeight;
        const xa = xOf(fa) * sc, xb = xOf(fb) * sc;
        const topOf = (y) => (y + 12) * sc;          // viewBox starts at y = -12
        let left, topPx;
        if (topOf(yTop) - ch - 8 >= 0) {
          left = (xa + xb) / 2 - cw / 2;
          topPx = topOf(yTop) - ch - 8;
        } else {
          topPx = Math.max(2, topOf(8));
          left = xa - cw - 10 >= 0 ? xa - cw - 10 : xb + 10;
        }
        call.style.left = `${Math.max(2, Math.min(rect.width - cw - 2, left))}px`;
        call.style.top = `${topPx}px`;
      };
      let dragFt = null;
      svgL.addEventListener("pointerdown", (e) => {
        if (e.button !== 0) return;
        const f = ftAt(e);
        if (f < 0 || f > lenFt) return;
        dragFt = f;
        this._distSel = { sid: props.sid, a: f, b: f };
        svgL.setPointerCapture(e.pointerId);
        drawSel();
      });
      svgL.addEventListener("pointermove", (e) => {
        if (dragFt === null) return;
        this._distSel = { sid: props.sid, a: dragFt, b: clampFt(ftAt(e)) };
        drawSel();
      });
      const endDrag = () => {
        if (dragFt === null) return;
        dragFt = null;
        if (this._distSel && Math.abs(this._distSel.a - this._distSel.b) < 5) this._distSel = null;
        drawSel();
      };
      svgL.addEventListener("pointerup", endDrag);
      svgL.addEventListener("pointercancel", endDrag);
      if (clearA) clearA.onclick = (e) => {
        e.preventDefault();
        this._distSel = null;
        drawSel();
      };
      drawSel();
    }
    const cvCb = host.querySelector(".dist-cv-cb");
    if (cvCb) cvCb.onchange = () => {
      this._showCV = cvCb.checked;
      this._renderDistribution(host, props, coords);
    };
    const speedCb = host.querySelector(".dist-speed-cb");
    if (speedCb) speedCb.onchange = () => {
      this._showSpeed = speedCb.checked;
      this._renderDistribution(host, props, coords);
    };
    for (const [cls, key] of [[".dist-wx-rain-cb", "_showWxRain"], [".dist-wx-snow-cb", "_showWxSnow"]]) {
      const cb = host.querySelector(cls);
      if (cb) cb.onchange = () => {
        this[key] = cb.checked;
        this._renderDistribution(host, props, coords);
      };
    }
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
        const ft = ((W - padR - px) / innerW) * domFt - ghFt;
        if (ft < 0 || ft > lenFt) { cur.style.display = "none"; return; }
        cur.style.display = "";
        curLine.setAttribute("x1", px.toFixed(1));
        curLine.setAttribute("x2", px.toFixed(1));
        curBox.setAttribute("x", (px - 29).toFixed(1));
        curText.setAttribute("x", px.toFixed(1));
        curText.textContent = `${Math.round(ft)} ft`;
        const spdEl = cur.querySelector(".dist-cursor-spd");
        if (spd && ySpd) {
          const bi = Math.floor(ft / d.bucket_ft);
          const v = spd[bi];
          if (v != null) {
            const mph = v * MPH;
            spdEl.setAttribute("x", (px + 7).toFixed(1));
            spdEl.setAttribute("y", (ySpd(mph) - 7).toFixed(1));
            const extra = [[wxRain, "rain"], [wxSnow, "snow"]]
              .filter(([a]) => a && a[bi] != null)
              .map(([a, lab]) => ` · ${lab} ${(a[bi] * MPH).toFixed(1)}`).join("");
            spdEl.textContent = `${mph.toFixed(1)} mph${extra}`;
          } else spdEl.textContent = "";
        } else if (spdEl) spdEl.textContent = "";
      });
      svg.addEventListener("mouseleave", () => { cur.style.display = "none"; });
    }
    // Right-click anywhere on the strip -> Street View at that spot along
    // the segment, camera facing the traffic light (travel direction).
    svg.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      const rect = svg.getBoundingClientRect();
      const px = (e.clientX - rect.left) * (W / rect.width);
      const ft = ((W - padR - px) / innerW) * domFt - ghFt;
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
      routes: ${routes.map((r) => `${this._routeLabel(r.r)} ${r.dir}`).join(", ")}<br>
      ${spec.label}${statBit}: ${v == null ? "—" : ((this.N.metric === "cv_increase" || spec.cond) && v > 0 ? "+" : "") + fmtValue(this.N.metric, v)} · ${Math.round(p.len_m)} m${this._cvHoverBit(p.sid)}${this._weatherHoverBit(p.sid)}`;
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

    // Open the panel at once with a spinner: the per-period table below
    // needs every period's shards, which on a first click (NYC: ~14 parts
    // per period) can take a while.
    const placeholder = document.createElement("div");
    placeholder.className = "nw-detail";
    placeholder.innerHTML = `
      <button class="nw-close">×</button>
      <h3>${cleanLabel(p.label)}</h3>
      <div class="nw-panel-loading"><span class="nw-spinner"></span> Loading segment…</div>`;
    placeholder.querySelector(".nw-close").onclick = () => {  // same as the real panel's ×
      placeholder.remove();
      this.map.highlight([]);
      this.S.network.selected = null;
      this.N.syncHash?.();
    };
    $("stage").appendChild(placeholder);

    // Per-period table under the current non-period filters.
    const rows = [];
    for (const period of ALL_PERIODS) {
      const acc = (await this.data.combine({ ...this.F, periods: [period] })).get(sid);
      rows.push(acc ?? null);
    }
    placeholder.remove();
    // another segment (or none) was selected while we loaded: drop this one
    if (this.S.network.selected !== sid) return;
    const M = this.data.meta;
    const cell = (v, suffix = "") => (Number.isFinite(v) ? `${v.toFixed(0)}${suffix}` : "—");

    const el = document.createElement("div");
    el.className = "nw-detail";
    const ffMph = p.t_ff_s > 0 ? ((p.len_m / p.t_ff_s) * 2.23694).toFixed(0) : "—";
    el.innerHTML = `
      <button class="nw-close">×</button>
      <h3>${cleanLabel(p.label)}</h3>
      <div class="nw-chips">${p.routes.map((r) => `<span class="chip">${this._routeLabel(r.r)} ${r.dir}</span>`).join("")}</div>
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
