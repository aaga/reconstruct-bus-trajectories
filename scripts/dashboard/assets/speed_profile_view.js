// D3 SVG speed-profile view. Owns the speed line, delay-band rectangles,
// feature ticks, the vertical hover cursor, and a tooltip-on-band overlay.
// Supports zoom/pan along the x-axis, which mirrors back to the map view
// via the State pub/sub.

import * as d3 from "https://cdn.jsdelivr.net/npm/d3@7.9.0/+esm";

const M_PER_MI = 1609.344;

// Band colors are now tied to the corresponding marker colors in
// map_view.js: dwell ↔ bus_stop (blue), signal_* ↔ traffic_signal (red),
// crossing ↔ ped_crossing_marked (teal). Slowdown has no map marker; we
// keep purple as a "residual / other" hue.
const BAND_COLORS = {
  dwell:               "#3a85d6",   // bus_stop blue
  dwell_near_signal:   "#3a85d6",   // same base; hatch overlay below
  signal_uniform:      "#cc0000",   // traffic_signal red
  signal_overflow:     "#7d1010",   // darker red — "queue spillback"
  crossing:            "#00897b",   // ped_crossing_marked teal
  slowdown:            "#b27ab2",   // residual purple
};
// Dwells flagged as "near a signal" get a diagonal-stripe pattern over a
// LIGHT blue base, distinguishing them from confident dwells without
// reading as a darker (more confident) color.
const BAND_PATTERN = {
  dwell_near_signal:   "url(#hatch-dwell-near)",
};

const LEGEND = [
  { kind: "dwell",             label: "Dwell (bus stop)" },
  { kind: "dwell_near_signal", label: "Dwell, near a signal" },
  { kind: "signal_uniform",    label: "Signal — uniform delay" },
  { kind: "signal_overflow",   label: "Signal — overflow / queue" },
  { kind: "crossing",          label: "Pedestrian crossing" },
  { kind: "slowdown",          label: "Other slowdown" },
];

const MARGIN = { top: 20, right: 18, bottom: 28, left: 50 };
const TICK_LANE_PX = 16;

export class SpeedProfileView {
  constructor(container, data, state) {
    this.container = container;
    this.data = data;
    this.state = state;
    this.featuresById = new Map(data.features.map(f => [f.id, f]));
    this.view = data.views[0];
    this.hoveredFeatureId = null;
    // Set of feature ids claimed by at least one delay band as
    // facility_id. When the "show only attributed" toggle is on, anything
    // outside this set is hidden in both the tick lane and on the map.
    this.attributedIds = new Set();
    for (const v of data.views || []) {
      for (const b of v.delay_bands || []) {
        if (b.facility_id) this.attributedIds.add(b.facility_id);
      }
    }
    this.hideUnattributed = false;

    const rect = container.getBoundingClientRect();
    this.width = rect.width;
    this.height = rect.height;

    this.svg = d3.select(container).append("svg")
      .attr("width", this.width)
      .attr("height", this.height)
      .style("display", "block");

    // -- Patterns + clip -------------------------------------------------
    const defs = this.svg.append("defs");
    // Light-blue base, slightly-darker stripe — visibly "blue with a
    // texture" but not darker than the regular dwell color. A second
    // pattern (`-hover`) uses saturated dwell-blue as the base and a
    // very dark stripe, so the band visibly darkens when it's hovered
    // (matches the fill-opacity bump that the solid bands get).
    const hatch = defs.append("pattern")
      .attr("id", "hatch-dwell-near")
      .attr("patternUnits", "userSpaceOnUse")
      .attr("width", 7).attr("height", 7)
      .attr("patternTransform", "rotate(45)");
    hatch.append("rect")
      .attr("width", 7).attr("height", 7)
      .attr("fill", "#bcd6ee");
    hatch.append("line")
      .attr("x1", 0).attr("y1", 0).attr("x2", 0).attr("y2", 7)
      .attr("stroke", "#3a85d6").attr("stroke-width", 2.2);
    const hatchHover = defs.append("pattern")
      .attr("id", "hatch-dwell-near-hover")
      .attr("patternUnits", "userSpaceOnUse")
      .attr("width", 7).attr("height", 7)
      .attr("patternTransform", "rotate(45)");
    hatchHover.append("rect")
      .attr("width", 7).attr("height", 7)
      .attr("fill", "#3a85d6");
    hatchHover.append("line")
      .attr("x1", 0).attr("y1", 0).attr("x2", 0).attr("y2", 7)
      .attr("stroke", "#1a3d6a").attr("stroke-width", 2.4);

    const plotW = this.width - MARGIN.left - MARGIN.right;
    const plotH = this.height - MARGIN.top - MARGIN.bottom - TICK_LANE_PX;
    defs.append("clipPath").attr("id", "plot-clip")
      .append("rect")
      .attr("width", plotW)
      .attr("height", plotH);
    this.plotW = plotW;
    this.plotH = plotH;

    // -- Scales ---------------------------------------------------------
    // Keep an unzoomed reference scale so the d3.zoom transform can rescale
    // it deterministically each event.
    this.x0 = d3.scaleLinear()
      .domain([0, data.shape.length_m / M_PER_MI])
      .range([0, plotW]);
    this.x = this.x0.copy();
    const v = this.view.speed_profile.speed_mph;
    const vMax = Math.max(30, Math.ceil(d3.max(v) / 5) * 5);
    this.y = d3.scaleLinear().domain([0, vMax]).range([plotH, 0]);

    // -- Layers ---------------------------------------------------------
    this.g = this.svg.append("g")
      .attr("transform", `translate(${MARGIN.left},${MARGIN.top})`);

    this.g.append("rect")
      .attr("class", "bg")
      .attr("width", plotW).attr("height", plotH)
      .attr("fill", "#fafbfc");

    this.g.append("line")
      .attr("class", "threshold")
      .attr("x1", 0).attr("x2", plotW)
      .attr("y1", this.y(5)).attr("y2", this.y(5))
      .attr("stroke", "#999").attr("stroke-dasharray", "2,3").attr("stroke-width", 1);

    this.bandsLayer = this.g.append("g")
      .attr("class", "bands")
      .attr("clip-path", "url(#plot-clip)");

    this.lineGen = d3.line()
      .x(d => this.x(d.distM / M_PER_MI))
      .y(d => this.y(d.v))
      .curve(d3.curveMonotoneX);
    this.linePath = this.g.append("path")
      .attr("class", "speed-line")
      .attr("clip-path", "url(#plot-clip)")
      .attr("fill", "none")
      .attr("stroke", this.view.color || "#222")
      .attr("stroke-width", 1.6);
    this._renderLine();
    this._renderBands();

    // Feature tick lane. Sits just below the plot; the triangle tips
    // extend UPWARD past the lane's top edge to overlap the bottom of
    // the plot, which is how the user wants them to "jut out" into the
    // graph. The clip rect is expanded a few px in -y so the tips
    // aren't chopped.
    this.tickG = this.svg.append("g")
      .attr("transform", `translate(${MARGIN.left},${MARGIN.top + plotH + 2})`)
      .attr("class", "ticks");
    defs.append("clipPath").attr("id", "tick-clip")
      .append("rect")
      .attr("x", 0)
      .attr("y", -10)              // allow ~10 px of upward overshoot
      .attr("width", plotW)
      .attr("height", TICK_LANE_PX + 10);
    this.tickG.attr("clip-path", "url(#tick-clip)");
    this._renderTicks();

    this.xAxisG = this.svg.append("g")
      .attr("transform", `translate(${MARGIN.left},${MARGIN.top + plotH + TICK_LANE_PX})`);
    this.yAxisG = this.svg.append("g")
      .attr("transform", `translate(${MARGIN.left},${MARGIN.top})`);
    this._renderAxes();

    this.cursor = this.g.append("line")
      .attr("class", "cursor")
      .attr("y1", 0).attr("y2", plotH)
      .attr("stroke", "#cc0000").attr("stroke-width", 1)
      .attr("display", "none");

    // Title overlay (sits over the plot top edge).
    this.svg.append("text")
      .attr("x", MARGIN.left).attr("y", 12)
      .attr("font-size", 11).attr("fill", "#444")
      .text(data.view_title || "");

    this._buildLegend();
    this._buildTooltip();

    // -- Mouse / zoom handling ------------------------------------------
    // The overlay rect captures mouse events for both d3.zoom (drag/wheel)
    // and cursor scrubbing (mousemove). They coexist: d3.zoom only
    // responds to drag/wheel, leaving plain mousemove for us.
    this.overlay = this.g.append("rect")
      .attr("class", "hover-overlay")
      .attr("width", plotW).attr("height", plotH)
      .attr("fill", "transparent")
      .style("cursor", "crosshair")
      .on("mousemove", (event) => this._onMouseMove(event))
      .on("mouseleave", () => {
        this.state.publish("dist:cleared", null);
        if (this.hoveredFeatureId != null) {
          this.state.publish("feature:cleared", null);
        }
        this._hideTooltip();
      });

    this.zoom = d3.zoom()
      .scaleExtent([1, 250])
      .translateExtent([[0, 0], [plotW, plotH]])
      .extent([[0, 0], [plotW, plotH]])
      .filter(event => {
        // Allow wheel + drag; reject double-click (which the user expects
        // to do nothing here).
        if (event.type === "dblclick") return false;
        return !event.button;
      })
      .on("zoom", (event) => this._onZoom(event));
    this.overlay.call(this.zoom);

    // -- State subscriptions --------------------------------------------
    state.subscribe("dist:hovered", ({ distM }) => this._showCursor(distM));
    state.subscribe("dist:cleared", () => this.cursor.attr("display", "none"));
    state.subscribe("feature:hovered", ({ featureId }) =>
      this._highlightBands(featureId));
    state.subscribe("feature:cleared", () => this._highlightBands(null));
    state.subscribe("range:changed", (e) => {
      if (e.source === "profile") return;   // ignore our own echo
      this._setRangeFromExternal(e.visibleDistRangeM);
    });
    state.subscribe("hideUnattributed:changed", ({ value }) => {
      this.hideUnattributed = value;
      this._renderTicks();
    });
  }

  // ---- rendering ------------------------------------------------------

  _renderLine() {
    const dist_m = this.view.speed_profile.dist_m;
    const speed = this.view.speed_profile.speed_mph;
    const pts = dist_m.map((d, i) => ({ distM: d, v: speed[i] }));
    this.linePath.datum(pts).attr("d", this.lineGen);
  }

  _renderBands() {
    const bands = this.view.delay_bands;
    const sel = this.bandsLayer.selectAll("rect.band")
      .data(bands, (d, i) => i);
    sel.exit().remove();
    sel.enter().append("rect")
      .attr("class", "band")
      .attr("y", 0)
      .attr("height", this.plotH)
      .merge(sel)
      .attr("x", d => this.x(d.dist_start_m / M_PER_MI))
      .attr("width", d => Math.max(
        1,
        this.x(d.dist_end_m / M_PER_MI) - this.x(d.dist_start_m / M_PER_MI)
      ))
      .attr("fill", d => BAND_PATTERN[d.category] || BAND_COLORS[d.category] || "#888")
      .attr("fill-opacity", d => BAND_PATTERN[d.category] ? 0.85 : 0.45)
      .attr("stroke", "none");
  }

  _renderTicks() {
    const TICK_COLORS = {
      traffic_signals:       "#cc0000",
      stop:                  "#f2c543",
      ped_crossing_signal:   "#7b3fa0",
      ped_crossing_marked:   "#00897b",
      bus_stop:              "#3a85d6",
    };
    // Triangle sizes — signals and bus stops get the prominent set; the
    // less-controlling features (stop sign, crossings) use a smaller
    // triangle so they recede a touch visually. Tip extends UP into the
    // plot region (negative y in the tickG-local frame).
    const SIZES = {
      big:   { halfW: 5.5, tipY: -7, baseY: TICK_LANE_PX - 1 },
      small: { halfW: 3.5, tipY: -3, baseY: TICK_LANE_PX - 1 },
    };
    const sizeFor = (kind) =>
      (kind === "traffic_signals" || kind === "bus_stop")
        ? SIZES.big : SIZES.small;

    // Clean up any old <line> ticks left from previous render passes.
    this.tickG.selectAll("line.tick").remove();

    const sel = this.tickG.selectAll("polygon.tick")
      .data(this.data.features, d => d.id);
    sel.exit().remove();
    sel.enter().append("polygon")
      .attr("class", "tick")
      .attr("stroke", "#fff")
      .attr("stroke-width", 0.6)
      .attr("stroke-linejoin", "round")
      // Let the overlay rect underneath receive the mousemove so hover
      // scrubbing keeps working when the cursor is over a triangle.
      .attr("pointer-events", "none")
      .merge(sel)
      .attr("points", d => {
        const cx = this.x(d.dist_m / M_PER_MI);
        const s = sizeFor(d.kind);
        return `${cx},${s.tipY} ${cx - s.halfW},${s.baseY} ${cx + s.halfW},${s.baseY}`;
      })
      .attr("fill", d => TICK_COLORS[d.kind] || "#888")
      .attr("opacity", 0.92)
      // Hidden when the "show only attributed" toggle is on AND this
      // feature isn't claimed by any delay band.
      .attr("display", d =>
        (this.hideUnattributed && !this.attributedIds.has(d.id))
          ? "none" : null
      );
  }

  _renderAxes() {
    const xAxis = d3.axisBottom(this.x)
      .ticks(8).tickFormat(d => `${d.toFixed(d < 10 ? 1 : 0)} mi`);
    this.xAxisG.call(xAxis);
    const yAxis = d3.axisLeft(this.y).ticks(5).tickFormat(d => `${d}`);
    this.yAxisG.call(yAxis);
    this.yAxisG.selectAll(".tick text").attr("font-size", 10);
    this.xAxisG.selectAll(".tick text").attr("font-size", 10);
    if (!this._yLabel) {
      this._yLabel = this.svg.append("text")
        .attr("transform", `translate(14, ${MARGIN.top + this.plotH / 2}) rotate(-90)`)
        .attr("text-anchor", "middle")
        .attr("font-size", 10).attr("fill", "#444")
        .text("speed (mph)");
    }
  }

  // ---- zoom handling --------------------------------------------------

  _onZoom(event) {
    const t = event.transform;
    this.x = t.rescaleX(this.x0);
    this._renderBands();
    this._renderTicks();
    this._renderAxes();
    this.linePath.attr("d", this.lineGen);
    // Echo to the map — but only when the event came from a real user
    // gesture, not from our own setRangeFromExternal call below.
    if (!this._suppressPublish) {
      const [loMi, hiMi] = this.x.domain();
      this.state.publish("range:changed", {
        visibleDistRangeM: [loMi * M_PER_MI, hiMi * M_PER_MI],
        source: "profile",
      });
    }
  }

  _setRangeFromExternal([loM, hiM]) {
    // Map drove the zoom. Compute the d3.zoom transform that maps the
    // unzoomed x0 scale onto this new domain, apply it WITHOUT echoing.
    const loMi = loM / M_PER_MI;
    const hiMi = hiM / M_PER_MI;
    const denom = this.x0(hiMi) - this.x0(loMi);
    if (!isFinite(denom) || denom <= 0) return;
    const k = this.plotW / denom;
    const tx = -k * this.x0(loMi);
    this._suppressPublish = true;
    this.overlay.call(this.zoom.transform,
      d3.zoomIdentity.translate(tx, 0).scale(k));
    this._suppressPublish = false;
  }

  // ---- hover handling -------------------------------------------------

  _showCursor(distM) {
    if (distM == null) return this.cursor.attr("display", "none");
    const xPx = this.x(distM / M_PER_MI);
    if (xPx < 0 || xPx > this.plotW) {
      return this.cursor.attr("display", "none");
    }
    this.cursor.attr("display", null).attr("x1", xPx).attr("x2", xPx);
  }

  _onMouseMove(event) {
    const [mx, my] = d3.pointer(event, this.g.node());
    const distMi = this.x.invert(mx);
    const distM = distMi * M_PER_MI;
    this.state.publish("dist:hovered", { distM, source: "profile" });

    const hits = this.view.delay_bands.filter(
      b => b.dist_start_m <= distM && b.dist_end_m >= distM
    );
    if (hits.length > 0) {
      const b = hits[0];
      if (b.facility_id !== this.hoveredFeatureId) {
        this.state.publish("feature:hovered", { featureId: b.facility_id });
      }
      this._showTooltip(b, event);
    } else {
      if (this.hoveredFeatureId != null) {
        this.state.publish("feature:cleared", null);
      }
      this._hideTooltip();
    }
  }

  _highlightBands(featureId) {
    this.hoveredFeatureId = featureId;
    this.bandsLayer.selectAll("rect.band")
      .attr("fill", (d) => {
        const active = featureId != null && d.facility_id === featureId;
        // Patterned bands swap to a darker pattern variant on hover so
        // the cross-hatch keeps reading as "ambiguous dwell" but visually
        // darkens like the solid bands do.
        if (BAND_PATTERN[d.category]) {
          return active ? "url(#hatch-dwell-near-hover)" : BAND_PATTERN[d.category];
        }
        return BAND_COLORS[d.category] || "#888";
      })
      .attr("fill-opacity", (d) => {
        if (featureId != null && d.facility_id === featureId) return 0.9;
        return BAND_PATTERN[d.category] ? 0.85 : 0.45;
      });
  }

  // ---- tooltip --------------------------------------------------------

  _buildTooltip() {
    this.tooltip = document.createElement("div");
    this.tooltip.className = "band-tooltip";
    this.tooltip.style.display = "none";
    this.container.appendChild(this.tooltip);
  }

  _showTooltip(band, event) {
    const feature = band.facility_id ? this.featuresById.get(band.facility_id) : null;
    // For features whose label starts with a kind prefix ("Signal @", "Bus stop:")
    // we strip the prefix so the tooltip reads like "Foster Avenue" rather than
    // "Signal @ Foster Avenue". cross_street already holds the bare name.
    const where = feature ? (feature.cross_street || feature.label) : null;
    const catLabel = {
      dwell: "Dwell",
      dwell_near_signal: "Dwell (near signal)",
      signal_uniform: "Signal — uniform",
      signal_overflow: "Signal — overflow",
      crossing: "Crossing yield",
      slowdown: "Other slowdown",
    }[band.category] || band.category;
    const sec = band.duration_s.toFixed(1);
    const facility = where ? `<div class="ttl-where">${escapeHtml(where)}</div>` : "";
    this.tooltip.innerHTML =
      `<div class="ttl-cat" style="color:${BAND_COLORS[band.category] || "#444"}">${catLabel}</div>` +
      facility +
      `<div class="ttl-dur">${sec}s delay</div>`;
    this.tooltip.style.display = "block";
    // Position next to the cursor. clientX/Y are page-relative; convert to
    // container-relative by subtracting the container's bounding rect.
    const rect = this.container.getBoundingClientRect();
    const px = event.clientX - rect.left + 12;
    const py = event.clientY - rect.top + 12;
    this.tooltip.style.left = `${px}px`;
    this.tooltip.style.top = `${py}px`;
  }

  _hideTooltip() {
    if (this.tooltip) this.tooltip.style.display = "none";
  }

  // ---- legend ---------------------------------------------------------

  _buildLegend() {
    const el = document.createElement("div");
    el.className = "profile-legend";
    // Toggle row at the top: hide features that didn't cause delay on
    // this trip. Drives MapView + SpeedProfileView via State.
    const toggleId = `toggle-${Math.random().toString(36).slice(2, 8)}`;
    const toggleHtml = `
      <div class="legend-toggle">
        <input id="${toggleId}" type="checkbox">
        <label for="${toggleId}">Hide features without delay</label>
      </div>`;
    el.innerHTML = toggleHtml + LEGEND.map(({ kind, label }) => {
      const isHatch = !!BAND_PATTERN[kind];
      const fill = isHatch ? "url(#leg-hatch-dwell-near)" : BAND_COLORS[kind];
      const opacity = isHatch ? 0.9 : 0.6;
      const swatchSvg = `<svg width="14" height="14" viewBox="0 0 14 14">
        ${isHatch ? `<defs><pattern id="leg-hatch-dwell-near"
              patternUnits="userSpaceOnUse" width="6" height="6"
              patternTransform="rotate(45)">
            <rect width="6" height="6" fill="#bcd6ee"/>
            <line x1="0" y1="0" x2="0" y2="6"
                  stroke="#3a85d6" stroke-width="2.2"/>
          </pattern></defs>` : ""}
        <rect width="14" height="14" fill="${fill}" fill-opacity="${opacity}"
              stroke="#666" stroke-width="0.5"/>
      </svg>`;
      return `<div class="legend-row">${swatchSvg}<span>${label}</span></div>`;
    }).join("");
    this.container.appendChild(el);
    // Hook the toggle checkbox after the element is in the DOM.
    const toggle = el.querySelector(`#${toggleId}`);
    toggle.addEventListener("change", (event) => {
      this.state.publish("hideUnattributed:changed", { value: event.target.checked });
    });
  }
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
