// D3 SVG view for the route-aggregate dashboard. Switches between two
// renderings of the same per-mile x-axis:
//
//   Segments  — stacked-bar decomposition per signal-to-signal segment
//               (mirrors figures/F2_corridor.png). No hover behavior
//               beyond the continuous distance cursor.
//   Stems     — two-sided stems per facility: mean delay up, buffer
//               (p95 − mean) down (mirrors figures/H_buffer_stem.png).
//               Hovering near a stem shows a tooltip and grows the
//               matching map dot.
//
// Both modes share:
//   - the same x scale (route miles)
//   - the d3.zoom pan/zoom, which echoes through the State as
//     range:changed so the map zooms in sync
//   - the red dist-cursor line driven by `dist:hovered`
//   - the ghost line for the open Street View click
//
// "Hide features without delay" filters stems where attributed=false
// (p95_min < 0.5 min, as decided in the build script). Segments mode is
// unaffected by that toggle.

import * as d3 from "https://cdn.jsdelivr.net/npm/d3@7.9.0/+esm";

const M_PER_MI = 1609.344;

// Segments-mode stack order, bottom → top. Colors mirror the matplotlib
// palette from F2_corridor.png as closely as possible while staying
// consistent with the band palette used elsewhere in the dashboard.
const SEG_STACK = [
  { key: "t_ff_min",                label: "Free-flow",         color: "#9bd4a2" },
  { key: "t_dwell_clean_min",       label: "Dwell",             color: "#3a85d6" },
  { key: "t_dwell_near_signal_min", label: "Dwell @ near-side", color: "#3a85d6", hatch: true },
  { key: "d_signal_uniform_min",    label: "Signal — uniform",  color: "#cc0000" },
  { key: "d_signal_overflow_min",   label: "Signal — overflow", color: "#7d1010" },
  { key: "d_crossing_min",          label: "Crossing",          color: "#d6b56a" },
  { key: "d_congestion_pos_min",    label: "Congestion",        color: "#b27ab2" },
];
// Rendered below the x=0 line as a thin negative-residual bar.
const SEG_NEG_KEY = "d_congestion_neg_min";

// Stems-mode color per facility kind. Same palette as map markers.
const KIND_COLOR = {
  traffic_signals:     "#cc0000",
  bus_stop:            "#3a85d6",
  ped_crossing_marked: "#00897b",
  ped_crossing_signal: "#7b3fa0",
  stop:                "#f2c543",
};
const KIND_LABEL = {
  traffic_signals:     "Signal",
  bus_stop:            "Bus stop",
  ped_crossing_marked: "Crosswalk",
  ped_crossing_signal: "Ped signal",
  stop:                "Stop sign",
};

const MARGIN = { top: 24, right: 18, bottom: 28, left: 56 };

export class DelayView {
  constructor(container, data, state) {
    this.container = container;
    this.data = data;
    this.state = state;
    this.featuresById = new Map(data.features.map(f => [f.id, f]));
    this.segments = data.segments || [];
    this.hoveredFeatureId = null;
    this.mode = "segments";       // "segments" | "stems"
    this.hideUnattributed = false;

    const rect = container.getBoundingClientRect();
    this.width = rect.width;
    this.height = rect.height;

    this.svg = d3.select(container).append("svg")
      .attr("width", this.width)
      .attr("height", this.height)
      .style("display", "block");

    const defs = this.svg.append("defs");
    // Hatch pattern reused for "Dwell @ near-side" (light blue base,
    // darker stripe), matching the speed-profile band style.
    const hatch = defs.append("pattern")
      .attr("id", "rt-hatch-dwell-near")
      .attr("patternUnits", "userSpaceOnUse")
      .attr("width", 6).attr("height", 6)
      .attr("patternTransform", "rotate(45)");
    hatch.append("rect").attr("width", 6).attr("height", 6).attr("fill", "#bcd6ee");
    hatch.append("line").attr("x1", 0).attr("y1", 0).attr("x2", 0).attr("y2", 6)
      .attr("stroke", "#3a85d6").attr("stroke-width", 2);

    const plotW = this.width - MARGIN.left - MARGIN.right;
    const plotH = this.height - MARGIN.top - MARGIN.bottom;
    defs.append("clipPath").attr("id", "delay-plot-clip")
      .append("rect").attr("width", plotW).attr("height", plotH);
    this.plotW = plotW;
    this.plotH = plotH;

    // ---- x scale (miles), shared by both modes ----
    this.x0 = d3.scaleLinear()
      .domain([0, data.shape.length_m / M_PER_MI])
      .range([0, plotW]);
    this.x = this.x0.copy();

    // ---- precompute y-domains so mode switches don't rescale ----
    this._computeStackTotals();
    this._computeStemRange();

    this.g = this.svg.append("g")
      .attr("transform", `translate(${MARGIN.left},${MARGIN.top})`);

    this.g.append("rect")
      .attr("class", "bg")
      .attr("width", plotW).attr("height", plotH)
      .attr("fill", "#fafbfc");

    // Per-mode plot layers. Visibility is toggled in _setMode.
    this.segLayer = this.g.append("g")
      .attr("class", "seg-layer")
      .attr("clip-path", "url(#delay-plot-clip)");
    this.stemLayer = this.g.append("g")
      .attr("class", "stem-layer")
      .attr("clip-path", "url(#delay-plot-clip)")
      .style("display", "none");

    // Zero-line for the stems mode (rendered under the stems, hidden in
    // segments mode where y=0 is the plot floor).
    this.zeroLine = this.stemLayer.append("line")
      .attr("x1", 0).attr("x2", plotW)
      .attr("stroke", "#333").attr("stroke-width", 0.8);

    // Cursor + ghost lines — shared.
    this.cursor = this.g.append("line")
      .attr("class", "cursor")
      .attr("y1", 0).attr("y2", plotH)
      .attr("stroke", "#cc0000").attr("stroke-width", 1)
      .attr("display", "none");
    this.ghostLine = this.g.append("line")
      .attr("class", "ghost-cursor")
      .attr("y1", 0).attr("y2", plotH)
      .attr("stroke", "#cc0000").attr("stroke-width", 1.5)
      .attr("stroke-dasharray", "4,3")
      .attr("opacity", 0.55)
      .attr("display", "none");
    this.ghostDistM = null;

    // Axes
    this.xAxisG = this.svg.append("g")
      .attr("transform", `translate(${MARGIN.left},${MARGIN.top + plotH})`);
    this.yAxisG = this.svg.append("g")
      .attr("transform", `translate(${MARGIN.left},${MARGIN.top})`);
    this._yLabel = this.svg.append("text")
      .attr("transform", `translate(14, ${MARGIN.top + plotH / 2}) rotate(-90)`)
      .attr("text-anchor", "middle")
      .attr("font-size", 10).attr("fill", "#444");

    this.svg.append("text")
      .attr("x", MARGIN.left).attr("y", 14)
      .attr("font-size", 11).attr("fill", "#444")
      .text(data.view_title || "");

    this._renderSegments();
    this._renderStems();
    this._renderAxes();

    this._buildLegend();
    this._buildTooltip();

    // Mouse + zoom — same construction as the speed-profile view.
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
      })
      .on("click", (event) => this._onClick(event));

    this.zoom = d3.zoom()
      .scaleExtent([1, 250])
      .translateExtent([[0, 0], [plotW, plotH]])
      .extent([[0, 0], [plotW, plotH]])
      .filter(event => event.type !== "dblclick" && !event.button)
      .on("zoom", (event) => this._onZoom(event));
    this.overlay.call(this.zoom);

    // State subscriptions
    state.subscribe("dist:hovered", ({ distM }) => this._showCursor(distM));
    state.subscribe("dist:cleared", () => this.cursor.attr("display", "none"));
    state.subscribe("feature:hovered", ({ featureId }) =>
      this._highlightFeature(featureId));
    state.subscribe("feature:cleared", () => this._highlightFeature(null));
    state.subscribe("range:changed", (e) => {
      if (e.source === "profile") return;
      this._setRangeFromExternal(e.visibleDistRangeM);
    });
    state.subscribe("hideUnattributed:changed", ({ value }) => {
      this.hideUnattributed = value;
      if (this._hideCheckbox && this._hideCheckbox.checked !== value) {
        this._hideCheckbox.checked = value;
      }
      this._renderStems();
    });
    state.subscribe("delayMode:changed", ({ value }) => this._setMode(value));
    state.subscribe("streetview:open", ({ distM }) => {
      this.ghostDistM = distM;
      this._updateGhost();
    });
    state.subscribe("streetview:close", () => {
      this.ghostDistM = null;
      this._updateGhost();
    });
  }

  // -------- y-domain precomputation -----------------------------------

  _computeStackTotals() {
    // Y max for Segments mode = the tallest segment's sum of all positive
    // stack components. Min is the deepest negative residual (typically
    // tiny, but we include it so bars below the 0 line aren't clipped).
    let maxPos = 0, maxNeg = 0;
    for (const s of this.segments) {
      let sum = 0;
      for (const c of SEG_STACK) sum += s[c.key] || 0;
      if (sum > maxPos) maxPos = sum;
      const neg = s[SEG_NEG_KEY] || 0;
      if (neg > maxNeg) maxNeg = neg;
    }
    // Round headroom to the nearest 0.5 min so the axis ticks read cleanly.
    const top = Math.max(1, Math.ceil(maxPos * 2) / 2 + 0.25);
    const bot = Math.max(0, Math.ceil(maxNeg * 2) / 2);
    this._segYMax = top;
    this._segYMin = -bot;
    this.ySeg = d3.scaleLinear()
      .domain([this._segYMin, this._segYMax])
      .range([this.plotH, 0]);
  }

  _computeStemRange() {
    let maxMean = 0, maxBuf = 0;
    for (const f of this.data.features) {
      const m = f.mean_min || 0;
      const b = f.buffer_min || 0;
      if (m > maxMean) maxMean = m;
      if (b > maxBuf) maxBuf = b;
    }
    // 20% headroom on top for annotation, 10% on the bottom (no labels
    // currently rendered there).
    this._stemYMax = Math.max(0.5, maxMean * 1.2);
    this._stemYMin = -Math.max(0.5, maxBuf * 1.1);
    this.yStem = d3.scaleLinear()
      .domain([this._stemYMin, this._stemYMax])
      .range([this.plotH, 0]);
  }

  _yActive() {
    return this.mode === "stems" ? this.yStem : this.ySeg;
  }

  // -------- segment rendering -----------------------------------------

  _renderSegments() {
    const y = this.ySeg;
    // For each stack category we compute the running bottom in domain
    // units (minutes), then map both edges through the y scale.
    const enterParent = this.segLayer.selectAll("g.seg-stack")
      .data(this.segments, d => d.seg_id);
    enterParent.exit().remove();
    const stack = enterParent.enter().append("g")
      .attr("class", "seg-stack")
      .merge(enterParent);

    stack.each((d, i, nodes) => {
      const g = d3.select(nodes[i]);
      // Bar x extent comes from the active x scale on the segment's
      // distance bounds; both ends inset slightly so adjacent bars don't
      // touch (matches matplotlib's `width = seg_widths * 0.92`).
      const xMiStart = (d.dist_start_m || 0) / M_PER_MI;
      const xMiEnd   = (d.dist_end_m   || 0) / M_PER_MI;
      const x0 = this.x(xMiStart);
      const x1 = this.x(xMiEnd);
      const span = x1 - x0;
      const inset = span * 0.04;
      const bx = x0 + inset;
      const bw = Math.max(0.5, span - 2 * inset);

      // Stacked positive components.
      let cum = 0;
      const bars = [];
      for (const c of SEG_STACK) {
        const v = d[c.key] || 0;
        if (v <= 0) continue;
        bars.push({ y0: cum, y1: cum + v, color: c.color, hatch: !!c.hatch, key: c.key });
        cum += v;
      }
      const neg = d[SEG_NEG_KEY] || 0;
      const negBars = neg > 0 ? [{ y0: -neg, y1: 0 }] : [];

      const posSel = g.selectAll("rect.pos").data(bars);
      posSel.exit().remove();
      posSel.enter().append("rect").attr("class", "pos")
        .attr("stroke", "white").attr("stroke-width", 0.3)
        .merge(posSel)
        .attr("x", bx)
        .attr("width", bw)
        .attr("y", b => y(b.y1))
        .attr("height", b => Math.max(0, y(b.y0) - y(b.y1)))
        .attr("fill", b => b.hatch ? "url(#rt-hatch-dwell-near)" : b.color);

      const negSel = g.selectAll("rect.neg").data(negBars);
      negSel.exit().remove();
      negSel.enter().append("rect").attr("class", "neg")
        .attr("stroke", "white").attr("stroke-width", 0.3)
        .attr("fill", "#bbb").attr("fill-opacity", 0.6)
        .merge(negSel)
        .attr("x", bx)
        .attr("width", bw)
        .attr("y", b => y(0))
        .attr("height", b => Math.max(0, y(b.y0) - y(0)));
    });
  }

  // -------- stem rendering --------------------------------------------

  _renderStems() {
    const y = this.yStem;
    // Visible set: features with any delay magnitude, optionally further
    // restricted by attributed when the toggle is on.
    const visible = this.data.features.filter(f => {
      const m = f.mean_min || 0;
      const b = f.buffer_min || 0;
      if (m <= 0 && b <= 0) return false;
      if (this.hideUnattributed && f.attributed === false) return false;
      return true;
    });

    const sel = this.stemLayer.selectAll("g.stem")
      .data(visible, d => d.id);
    sel.exit().remove();
    const enter = sel.enter().append("g")
      .attr("class", "stem")
      // Triangles in the profile view keep pointer-events:none for the
      // same reason — let the overlay receive mousemove unimpeded.
      .style("pointer-events", "none");
    enter.append("line").attr("class", "mean-line");
    enter.append("circle").attr("class", "mean-dot");
    enter.append("line").attr("class", "buf-line");
    enter.append("circle").attr("class", "buf-dot");
    const merged = enter.merge(sel);

    merged.each((d, i, nodes) => {
      const g = d3.select(nodes[i]);
      const xPx = this.x((d.dist_m || 0) / M_PER_MI);
      const color = KIND_COLOR[d.kind] || "#888";
      const isHover = (this.hoveredFeatureId === d.id);
      const meanR = isHover ? 5.5 : 3.0;
      const bufR = isHover ? 4.5 : 2.3;
      const mean = d.mean_min || 0;
      const buf = d.buffer_min || 0;

      g.select(".mean-line")
        .attr("x1", xPx).attr("x2", xPx)
        .attr("y1", y(0)).attr("y2", y(mean))
        .attr("stroke", color).attr("stroke-width", 1.4)
        .attr("opacity", mean > 0 ? 0.9 : 0);
      g.select(".mean-dot")
        .attr("cx", xPx).attr("cy", y(mean))
        .attr("r", meanR)
        .attr("fill", color).attr("stroke", "#111").attr("stroke-width", 0.5)
        .attr("opacity", mean > 0 ? 1 : 0);

      g.select(".buf-line")
        .attr("x1", xPx).attr("x2", xPx)
        .attr("y1", y(0)).attr("y2", y(-buf))
        .attr("stroke", color).attr("stroke-width", 1.2)
        .attr("opacity", buf > 0 ? 0.55 : 0);
      g.select(".buf-dot")
        .attr("cx", xPx).attr("cy", y(-buf))
        .attr("r", bufR)
        .attr("fill", color).attr("stroke", "#111").attr("stroke-width", 0.4)
        .attr("opacity", buf > 0 ? 0.7 : 0);
    });

    this.zeroLine
      .attr("y1", y(0)).attr("y2", y(0));
    this._visibleStems = visible;
  }

  // -------- axes -------------------------------------------------------

  _renderAxes() {
    const xAxis = d3.axisBottom(this.x).ticks(8)
      .tickFormat(d => `${d.toFixed(d < 10 ? 1 : 0)} mi`);
    this.xAxisG.call(xAxis);
    this.xAxisG.selectAll(".tick text").attr("font-size", 10);

    const y = this._yActive();
    const yAxis = d3.axisLeft(y).ticks(6)
      // In stems mode the bottom half is *displayed* as positive minutes
      // (buffer is a magnitude). Format abs value either way.
      .tickFormat(v => `${Math.abs(v).toFixed(v === 0 ? 0 : 1)}`);
    this.yAxisG.call(yAxis);
    this.yAxisG.selectAll(".tick text").attr("font-size", 10);

    this._yLabel.text(this.mode === "stems"
      ? "mean ↑  /  buffer ↓  (min)"
      : "mean time per trip (min)");
  }

  // -------- zoom / cursor ---------------------------------------------

  _onZoom(event) {
    this.x = event.transform.rescaleX(this.x0);
    this._renderSegments();
    this._renderStems();
    this._renderAxes();
    this._updateGhost();
    if (!this._suppressPublish) {
      const [lo, hi] = this.x.domain();
      this.state.publish("range:changed", {
        visibleDistRangeM: [lo * M_PER_MI, hi * M_PER_MI],
        source: "profile",
      });
    }
  }

  _setRangeFromExternal([loM, hiM]) {
    const loDom = loM / M_PER_MI, hiDom = hiM / M_PER_MI;
    const denom = this.x0(hiDom) - this.x0(loDom);
    if (!isFinite(denom) || denom <= 0) return;
    const k = this.plotW / denom;
    const tx = -k * this.x0(loDom);
    this._suppressPublish = true;
    this.overlay.call(this.zoom.transform,
      d3.zoomIdentity.translate(tx, 0).scale(k));
    this._suppressPublish = false;
  }

  _showCursor(distM) {
    if (distM == null) return this.cursor.attr("display", "none");
    const xPx = this.x(distM / M_PER_MI);
    if (xPx < 0 || xPx > this.plotW) {
      return this.cursor.attr("display", "none");
    }
    this.cursor.attr("display", null).attr("x1", xPx).attr("x2", xPx);
  }

  _updateGhost() {
    if (this.ghostDistM == null) {
      return this.ghostLine.attr("display", "none");
    }
    const xPx = this.x(this.ghostDistM / M_PER_MI);
    if (xPx < 0 || xPx > this.plotW) {
      return this.ghostLine.attr("display", "none");
    }
    this.ghostLine.attr("display", null).attr("x1", xPx).attr("x2", xPx);
  }

  // -------- click + mouse ---------------------------------------------

  _onClick(event) {
    const [mx] = d3.pointer(event, this.g.node());
    const distM = this.x.invert(mx) * M_PER_MI;
    this.state.publish("streetview:open", { distM });
  }

  _onMouseMove(event) {
    const [mx] = d3.pointer(event, this.g.node());
    const distM = this.x.invert(mx) * M_PER_MI;
    this.state.publish("dist:hovered", { distM, source: "profile" });

    // Stem hover detection (stems mode only — segments mode has no
    // per-element hover, per the user spec).
    if (this.mode === "stems") {
      const f = this._nearestStemFeature(mx);
      if (f) {
        if (f.id !== this.hoveredFeatureId) {
          this.state.publish("feature:hovered", { featureId: f.id });
        }
        this._showStemTooltip(f, event);
        return;
      }
    }
    if (this.hoveredFeatureId != null) {
      this.state.publish("feature:cleared", null);
    }
    this._hideTooltip();
  }

  _nearestStemFeature(xPx) {
    // 8 px horizontal tolerance — stems are zero-width so we need a wider
    // catch radius than the band-hover overlap test.
    const visible = this._visibleStems || [];
    let best = null, bestDx = 8;
    for (const f of visible) {
      const dx = Math.abs(this.x((f.dist_m || 0) / M_PER_MI) - xPx);
      if (dx < bestDx) { bestDx = dx; best = f; }
    }
    return best;
  }

  _highlightFeature(featureId) {
    if (this.hoveredFeatureId === featureId) return;
    this.hoveredFeatureId = featureId;
    if (this.mode === "stems") this._renderStems();
  }

  // -------- mode switch -----------------------------------------------

  _setMode(mode) {
    if (mode !== "segments" && mode !== "stems") return;
    if (this.mode === mode) return;
    this.mode = mode;
    this.segLayer.style("display", mode === "segments" ? null : "none");
    this.stemLayer.style("display", mode === "stems" ? null : "none");
    if (this._modeRadios) {
      this._modeRadios.forEach(r => { r.checked = (r.value === mode); });
    }
    if (this._segSection)
      this._segSection.style.display = (mode === "segments") ? "" : "none";
    if (this._stemSection)
      this._stemSection.style.display = (mode === "stems") ? "" : "none";
    this._renderAxes();
    this._hideTooltip();
  }

  // -------- tooltip ---------------------------------------------------

  _buildTooltip() {
    this.tooltip = document.createElement("div");
    this.tooltip.className = "band-tooltip";
    this.tooltip.style.display = "none";
    this.container.appendChild(this.tooltip);
  }

  _showStemTooltip(feature, event) {
    const color = KIND_COLOR[feature.kind] || "#444";
    const kindLab = KIND_LABEL[feature.kind] || feature.kind;
    const name = feature.cross_street || feature.label || "";
    const mean = (feature.mean_min || 0).toFixed(2);
    const buf = (feature.buffer_min || 0).toFixed(2);
    this.tooltip.innerHTML =
      `<div class="ttl-cat" style="color:${color}">${kindLab}</div>` +
      (name ? `<div class="ttl-where">${escapeHtml(name)}</div>` : "") +
      `<div class="ttl-dur">mean ${mean} min · buffer ${buf} min</div>`;
    this.tooltip.style.display = "block";
    // Anchor above the cursor — stems extend the full pane height, but
    // the dot at the top of a tall stem ends up near the cursor's y, so
    // an above-and-right placement keeps the tooltip clear.
    const rect = this.container.getBoundingClientRect();
    const h = this.tooltip.offsetHeight;
    this.tooltip.style.left = `${event.clientX - rect.left + 12}px`;
    this.tooltip.style.top = `${event.clientY - rect.top - h - 8}px`;
  }

  _hideTooltip() {
    if (this.tooltip) this.tooltip.style.display = "none";
  }

  // -------- legend ----------------------------------------------------

  _buildLegend() {
    const el = document.createElement("div");
    el.className = "profile-legend";

    const modeName = `delaymode-${Math.random().toString(36).slice(2, 8)}`;
    const hideId = `delaytoggle-${Math.random().toString(36).slice(2, 8)}`;
    const modeHtml = `
      <div class="legend-toggle">
        <span class="legend-toggle-label">view:</span>
        <label class="legend-radio">
          <input type="radio" name="${modeName}" value="segments" checked>
          <span>Segments <span class="legend-shortcut">(B)</span></span>
        </label>
        <label class="legend-radio">
          <input type="radio" name="${modeName}" value="stems">
          <span>Stems <span class="legend-shortcut">(E)</span></span>
        </label>
      </div>`;
    const hideHtml = `
      <div class="legend-toggle">
        <input id="${hideId}" type="checkbox">
        <label for="${hideId}">Hide features without delay <span class="legend-shortcut">(H)</span></label>
      </div>`;

    // Mode-specific swatch rows. The segments stack and the stem-kind
    // legend are rendered in two separate sections; the inactive one is
    // hidden via class.
    const segRows = SEG_STACK.map(c => {
      const swatch = c.hatch
        ? `<svg width="14" height="14" viewBox="0 0 14 14">
            <defs><pattern id="leg-${c.key}" patternUnits="userSpaceOnUse"
                width="6" height="6" patternTransform="rotate(45)">
              <rect width="6" height="6" fill="#bcd6ee"/>
              <line x1="0" y1="0" x2="0" y2="6" stroke="#3a85d6" stroke-width="2"/>
            </pattern></defs>
            <rect width="14" height="14" fill="url(#leg-${c.key})"
                  stroke="#666" stroke-width="0.5"/>
          </svg>`
        : `<svg width="14" height="14" viewBox="0 0 14 14">
            <rect width="14" height="14" fill="${c.color}" stroke="#666" stroke-width="0.5"/>
          </svg>`;
      return `<div class="legend-row">${swatch}<span>${c.label}</span></div>`;
    }).join("");
    const stemRows = Object.entries(KIND_LABEL).map(([kind, lab]) => {
      const c = KIND_COLOR[kind];
      return `<div class="legend-row">
        <svg width="14" height="14"><circle cx="7" cy="7" r="4"
            fill="${c}" stroke="#111" stroke-width="0.5"/></svg>
        <span>${lab}</span>
      </div>`;
    }).join("");

    el.innerHTML =
      modeHtml + hideHtml +
      `<div class="legend-section legend-segments">${segRows}</div>` +
      `<div class="legend-section legend-stems" style="display:none">${stemRows}</div>`;

    this.container.appendChild(el);
    this._hideCheckbox = el.querySelector(`#${hideId}`);
    this._modeRadios = el.querySelectorAll(`input[name="${modeName}"]`);
    this._segSection = el.querySelector(".legend-segments");
    this._stemSection = el.querySelector(".legend-stems");
    this._hideCheckbox.addEventListener("change", (event) => {
      this.state.publish("hideUnattributed:changed", { value: event.target.checked });
    });
    this._modeRadios.forEach(input => {
      input.addEventListener("change", (event) => {
        if (event.target.checked) {
          this.state.publish("delayMode:changed", { value: event.target.value });
        }
      });
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
