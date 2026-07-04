// Speed tab: speed vs time (or distance), with four stacked delay-bar rows
// (AVL / Observed / High-Freq / Low-Freq) and a vertical cursor that drives the
// source-coloured bus marker(s) on the map; click pans, double-click opens
// Street View. Body is the original renderSpeed()/tooltip helpers, keyed off S.

import * as d3 from "https://cdn.jsdelivr.net/npm/d3@7.9.0/+esm";
import {
  $, makeSvg, installInteraction, interp, color, fmtClock,
  defaultXExtent, defaultDistExtentM, getSource,
} from "../chart_util.js";

// delay_row.key → the checkbox toggle key in index.html.
const ROW_TOGGLE = { avl: "dAVL", observed: "dWeb", phone: "dPhone", r2: "dR2" };
import { distToLonLat } from "../projection.js";

export class SpeedView {
  constructor(S) { this.S = S; }

  // Position the bus marker(s) for an x-value in the current speed units.
  placeBusesAt(v) {
    const S = this.S;
    if (!S.busLo || v == null) return;
    if (S.speedX === "distance") { // distance is the shared invariant -> one bus
      this.placeBus(S.busLo, v, true);
      this.placeBus(S.busHi, null, false);
    } else {
      this.placeBus(S.busHi, S.tToDist ? S.tToDist(v) : null, S.toggles.busHi);
      this.placeBus(S.busLo, S.r2ToDist ? S.r2ToDist(v) : null, S.toggles.busLo && !!getSource(S.trip, "r2"));
    }
  }

  // Pan (not zoom) the map to center a route distance.
  panMapTo(distM) {
    const S = this.S;
    if (!S.mapView || distM == null || Number.isNaN(distM)) return;
    S.mapView.map.panTo(
      distToLonLat(distM, S.trip.shape.polyline_lonlat, S.trip.shape.cumdist_m),
      { duration: 500 });
  }

  // Place a bus marker at a route distance, or hide it.
  placeBus(marker, distM, show) {
    const S = this.S;
    if (!marker) return;
    const el = marker.getElement();
    if (!show || distM == null || Number.isNaN(distM)) { el.style.display = "none"; return; }
    marker.setLngLat(distToLonLat(distM, S.trip.shape.polyline_lonlat, S.trip.shape.cumdist_m));
    el.style.display = "";
  }

  showTip(e, rowLabel, d) {
    const t = this.S.trip;
    const dur = d.t_end != null ? `${Math.round(d.t_end - d.t_start)}s` : "open";
    $("tooltip").innerHTML =
      `<b>${rowLabel}</b> · ${d.category}<br>${d.label || "—"}<br>` +
      `${fmtClock(t, d.t_start)}${d.t_end != null ? "–" + fmtClock(t, d.t_end) : ""} (${dur})`;
    $("tooltip").classList.remove("hidden");
    $("tooltip").style.left = e.clientX + 12 + "px";
    $("tooltip").style.top = e.clientY + 12 + "px";
  }

  hideTip() { $("tooltip").classList.add("hidden"); }

  // Rich AVL stop tooltip: dwell + passenger load before/after + door flows.
  showAvlTip(e, d) {
    const t = this.S.trip;
    const head =
      `<b>AVL · ${d.event_desc}</b> (event ${d.event_type})<br>` +
      `${d.label}${d.stop_seq != null ? " · seq " + d.stop_seq : ""}<br>` +
      `${fmtClock(t, d.t_start)}–${fmtClock(t, d.t_end)} · dwell ${d.dwell_s}s`;
    let pax;
    if (d.flow > 0) {
      pax =
        `Load: ${d.load_before ?? "?"} → ${d.load_after ?? "?"}<br>` +
        `On ${d.on_total} (front ${d.on_front} / rear ${d.on_rear}) · ` +
        `Off ${d.off_total} (front ${d.off_front} / rear ${d.off_rear})<br>` +
        `Flow ${d.flow}` +
        (d.dwell_per_pax != null ? ` · ${d.dwell_per_pax}s / passenger` : "");
    } else {
      pax = `Load: ${d.load_after ?? "?"} · no passenger activity`;
    }
    $("tooltip").innerHTML = head + "<hr style='border:none;border-top:1px solid #555;margin:4px 0'>" + pax;
    $("tooltip").classList.remove("hidden");
    $("tooltip").style.left = e.clientX + 12 + "px";
    $("tooltip").style.top = e.clientY + 12 + "px";
  }

  render() {
    const self = this;
    const S = this.S;
    const t = S.trip;
    const fmt = (tSec) => fmtClock(t, tSec);
    const distMode = S.speedX === "distance";
    const M = { l: 56, r: 16, t: 14 };
    const { svg, width, height } = makeSvg();
    const rowH = 26, rowGap = 6;
    // AVL covers the whole trip; in distance mode keep only stops the observed
    // trajectory can place (within its time span), since pre-boarding stops have
    // no route distance. In time mode show them all (full-trip view reveals them).
    const pT = getSource(t, "phone").curve.t;
    // Build the delay-bar rows from the unified delay_rows[]. Each row's src is
    // the curve of its source (for distance-mode x placement); AVL rows get the
    // rich passenger tooltip and, in distance mode, are clipped to the observed
    // trajectory's time span (pre-boarding stops have no route distance).
    const rows = t.delay_rows.map((dr) => {
      const source = getSource(t, dr.source_key);
      const avl = dr.role === "avl";
      let items = dr.items || [];
      if (distMode && avl) items = items.filter((b) => b.t_start >= pT[0] && b.t_start <= pT[pT.length - 1]);
      return { key: ROW_TOGGLE[dr.key] || dr.key, label: dr.label, delays: items, src: source ? source.curve : null, avl };
    });
    const stripH = rows.length * (rowH + rowGap);
    const axisY = height - 24;
    const speedH = axisY - stripH - 12;
    const rowTop = speedH + 12;

    let maxV = 5;
    for (const s of t.sources) maxV = Math.max(maxV, d3.max(s.curve.speed_mph));
    const xFull = distMode ? defaultDistExtentM(S) : defaultXExtent(S);
    const view = S.view.speed || (S.view.speed = { x: xFull.slice() });
    const fmtX = distMode ? (v) => (v / 1000).toFixed(1) : fmt;
    // Map a delay's time onto the current x-axis (distance via its own source).
    const delayX = (row, tSec) => (distMode ? interp(row.src.t, row.src.dist_m, tSec) : tSec);

    const x = d3.scaleLinear().range([M.l, width - M.r]);
    const y = d3.scaleLinear().domain([0, maxV]).nice().range([speedH, M.t]);

    svg.append("g").attr("class", "axis").attr("transform", `translate(${M.l},0)`).call(d3.axisLeft(y));
    svg.append("text").attr("transform", "rotate(-90)").attr("x", -(speedH / 2)).attr("y", 14)
      .attr("text-anchor", "middle").attr("font-size", 12).attr("fill", "#555").text("speed (mph)");
    svg.append("text").attr("x", (M.l + width - M.r) / 2).attr("y", height - 4)
      .attr("text-anchor", "middle").attr("font-size", 11).attr("fill", "#888")
      .text(distMode ? "distance along route (km)" : "time (Chicago)");
    rows.forEach((r, i) => {
      const ry = rowTop + i * (rowH + rowGap);
      svg.append("text").attr("class", "rowlabel").attr("x", M.l - 6).attr("y", ry + rowH / 2 + 3)
        .attr("text-anchor", "end").text(r.label);
    });

    const gGrid = svg.append("g").attr("class", "grid");
    const gPhone = svg.append("g");
    const gR2 = svg.append("g");
    const gRows = rows.map(() => svg.append("g"));
    const gAxisX = svg.append("g").attr("class", "axis").attr("transform", `translate(0,${axisY})`);
    svg.append("clipPath").attr("id", "clip-spd").append("rect")
      .attr("x", M.l).attr("y", M.t).attr("width", width - M.l - M.r).attr("height", axisY - M.t);
    for (const g of [gPhone, gR2, ...gRows]) g.attr("clip-path", "url(#clip-spd)");

    // 5 mph slowdown-detection threshold reference line (static; y is fixed).
    svg.append("line").attr("class", "threshold-line")
      .attr("x1", M.l).attr("x2", width - M.r).attr("y1", y(5)).attr("y2", y(5));
    svg.append("text").attr("class", "threshold-label")
      .attr("x", width - M.r - 2).attr("y", y(5) - 3).attr("text-anchor", "end").text("5 mph");

    function redraw() {
      x.domain(view.x);
      gAxisX.call(d3.axisBottom(x).ticks(8).tickFormat(fmtX));
      gGrid.attr("transform", `translate(0,${axisY})`)
        .call(d3.axisBottom(x).ticks(8).tickSize(-(axisY - M.t)).tickFormat(""));
      gGrid.selectAll(".domain").remove();

      drawSpeed(gPhone, getSource(t, "phone"), "phone", S.toggles.phoneSpeed);
      drawSpeed(gR2, getSource(t, "r2"), "r2", S.toggles.r2Speed);

      rows.forEach((r, i) => {
        const ry = rowTop + i * (rowH + rowGap);
        const g = gRows[i]; g.selectAll("*").remove();
        const on = S.toggles[r.key];
        g.append("rect").attr("x", M.l).attr("y", ry).attr("width", width - M.r - M.l)
          .attr("height", rowH).attr("fill", on ? "#fcfcfd" : "#f3f3f5").attr("stroke", "#eee");
        if (!on || (distMode && !r.src)) return;
        const x0 = (d) => x(delayX(r, d.t_start));
        const x1 = (d) => x(delayX(r, d.t_end ?? d.t_start + 5));
        const sel = g.selectAll("g.bar").data(r.delays.filter((d) => d.t_start != null))
          .join("g").attr("class", "bar");
        sel.append("rect").attr("class", "delaybar")
          .attr("x", x0).attr("y", ry + 2)
          .attr("width", (d) => Math.max(r.avl ? 2.5 : 1.5, x1(d) - x0(d)))
          .attr("height", rowH - 4).attr("fill", (d) => color(d.category))
          .on("mousemove", (e, d) => (r.avl ? self.showAvlTip(e, d) : self.showTip(e, r.label, d)))
          .on("mouseleave", () => self.hideTip());
        sel.append("text").attr("class", "delaytext")
          .attr("x", (d) => x0(d) + 4).attr("y", ry + rowH / 2 + 3)
          .text((d) => {
            const w = x1(d) - x0(d);
            const lab = r.avl && d.category === "avl_other" ? d.event_desc : (d.label || d.category);
            return w > 26 ? lab.slice(0, Math.floor(w / 6)) : "";
          });
      });
      if (S.lastV != null) showCursorVal(S.lastV); // keep cursor aligned through zoom/pan
    }

    function drawSpeed(g, src, cls, show) {
      g.selectAll("*").remove();
      if (!src || !show) return;
      const line = d3.line().x((d) => x(d[0])).y((d) => y(d[1]));
      const pts = src.curve.t.map((tt, i) => [distMode ? src.curve.dist_m[i] : tt, src.curve.speed_mph[i]]);
      g.append("path").attr("class", `curve ${cls}`).attr("d", line(pts));
    }

    // Vertical cursor (in current x units) that drives the map bus icon(s).
    const cursorLine = svg.append("line").attr("class", "chart-cursor")
      .attr("y1", M.t).attr("y2", axisY).style("display", "none");
    const showCursorVal = (v) => {
      const px = x(v);
      if (px < M.l || px > width - M.r) { cursorLine.style("display", "none"); return; }
      cursorLine.attr("x1", px).attr("x2", px).style("display", null);
    };
    S.speedCursor = {
      // map publishes a route distance; convert to this axis's units
      showDist: (dM) => showCursorVal(distMode ? dM : S.distToT(dM)),
      hide: () => cursorLine.style("display", "none"),
    };

    const node = svg.node();
    const toDistM = (v) => (distMode ? v : S.tToDist(v));
    // Hover: live preview of cursor + bus(es). They PERSIST after the cursor
    // leaves the chart (no mouseleave-hide), so you can study the map.
    svg.on("mousemove.cur", (e) => {
      const [px] = d3.pointer(e, node);
      if (px < M.l || px > width - M.r) return;
      S.lastV = x.invert(px);
      showCursorVal(S.lastV);
      self.placeBusesAt(S.lastV);
    });
    // Single click -> pan map to that location (keep zoom). Double click ->
    // Street View. A timer disambiguates the two.
    let clickTimer = null;
    svg.on("click.cur", (e) => {
      if (node._dragMoved) return; // was a pan-drag, not a click
      if (clickTimer) { clearTimeout(clickTimer); clickTimer = null; return; } // 2nd click
      const [px] = d3.pointer(e, node);
      if (px < M.l || px > width - M.r) return;
      const v = x.invert(px);
      clickTimer = setTimeout(() => { clickTimer = null; self.panMapTo(toDistM(v)); }, 250);
    });
    svg.on("dblclick.cur", (e) => {
      if (clickTimer) { clearTimeout(clickTimer); clickTimer = null; }
      const [px] = d3.pointer(e, node);
      if (px < M.l || px > width - M.r) return;
      S.mapState?.publish("streetview:open", { distM: toDistM(x.invert(px)) });
    });

    x.domain(view.x);
    installInteraction(svg,
      { x: { scale: x, full: xFull }, y: null },
      view,
      () => ({ x: true, y: false }), // speed: horizontal only
      false, redraw);
    redraw();
    // Restore the persisted cursor + buses after a (re)render.
    if (S.lastV != null) { showCursorVal(S.lastV); self.placeBusesAt(S.lastV); }
  }
}
