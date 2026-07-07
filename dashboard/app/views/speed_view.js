// Speed tab: speed vs time (or distance), with stacked delay-bar rows (AVL /
// Observed / High-Freq / Low-Freq) and a cursor that drops coloured dots on each
// speed curve and drives the bus marker(s) on the map. High-Freq = purple,
// Low-Freq = green. A map-hover cursor (or distance-mode cursor) is a single
// light-grey bus — the same route location for both sources.

import * as d3 from "https://cdn.jsdelivr.net/npm/d3@7.9.0/+esm";
import {
  $, makeSvg, installInteraction, interp, color, fmtClock,
  defaultXExtent, defaultDistExtentM, timeExtent, getSource, SRC_COLOR, BUS_GRAY,
} from "../chart_util.js";
import { distToLonLat } from "../projection.js";

// delay_row.key → the toggle that controls its row. AVL/Observed have their own
// checkboxes; the inferred rows follow whether that source's speed curve shows.
const ROW_TOGGLE = { avl: "dAVL", observed: "dWeb", phone: "phoneSpeed", r2: "r2Speed" };

export class SpeedView {
  constructor(S) { this.S = S; }

  // Move a bus marker to a route distance with a given colour (or hide it).
  setBus(marker, distM, col, show) {
    if (!marker) return;
    const el = marker.getElement();
    if (!show || distM == null || Number.isNaN(distM)) { el.style.display = "none"; return; }
    const body = el.querySelector("rect");
    if (body) body.setAttribute("fill", col);
    const leader = el.querySelector(".bus-leader");
    if (leader) leader.setAttribute("stroke", col);
    const tip = el.querySelector(".bus-tip");
    if (tip) tip.setAttribute("fill", col);
    marker.setLngLat(distToLonLat(distM, this.S.trip.shape.polyline_lonlat, this.S.trip.shape.cumdist_m));
    el.style.display = "";
  }

  // Position the bus marker(s) for a cursor spec:
  //   chart hover + time → two colour-coded buses at each source's position;
  //   chart hover + distance, or map hover → one grey bus (shared location).
  placeBuses(spec) {
    const S = this.S;
    if (!S.busHi) return;
    const distMode = S.speedX === "distance";
    const phoneOn = S.toggles.phoneSpeed;
    const r2On = S.toggles.r2Speed && !!getSource(S.trip, "r2");
    if (spec.kind === "chart" && !distMode) {
      this.setBus(S.busHi, phoneOn && S.tToDist ? S.tToDist(spec.v) : null, SRC_COLOR.phone, phoneOn);
      this.setBus(S.busLo, r2On && S.r2ToDist ? S.r2ToDist(spec.v) : null, SRC_COLOR.r2, r2On);
    } else {
      const distM = spec.kind === "map" ? spec.distM : spec.v; // distance-mode v is a distance
      this.setBus(S.busHi, distM, BUS_GRAY, phoneOn || r2On);
      this.setBus(S.busLo, null, BUS_GRAY, false);
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
    const pT = getSource(t, "phone").curve.t;
    // Rows from the unified delay_rows[]; AVL gets the rich tooltip and, in
    // distance mode, is clipped to the observed trajectory's time span.
    const rows = t.delay_rows.map((dr) => {
      const source = getSource(t, dr.source_key);
      const avl = dr.role === "avl";
      let items = dr.items || [];
      if (distMode && avl) items = items.filter((b) => b.t_start >= pT[0] && b.t_start <= pT[pT.length - 1]);
      // Inferred rows are tinted in their source colour (magenta/green) when shown.
      const srcColor = dr.role === "inferred" ? SRC_COLOR[dr.source_key] : null;
      return { key: ROW_TOGGLE[dr.key] || dr.key, label: dr.label, delays: items, src: source ? source.curve : null, avl, srcColor };
    });
    const stripH = rows.length * (rowH + rowGap);
    const axisY = height - 24;
    const speedH = axisY - stripH - 12;
    const rowTop = speedH + 12;

    let maxV = 5;
    for (const s of t.sources) maxV = Math.max(maxV, d3.max(s.curve.speed_mph));
    // Default view crops to the primary-source window; the pan/zoom CLAMP is the
    // whole trip, so you can scroll past it into the low-freq-only region.
    const xDefault = distMode ? defaultDistExtentM(S) : defaultXExtent(S);
    let fullDist = 0; for (const s of t.sources) fullDist = Math.max(fullDist, d3.max(s.curve.dist_m));
    const xClamp = distMode ? [0, fullDist] : timeExtent(t);
    const view = S.view.speed || (S.view.speed = { x: xDefault.slice() });
    const fmtX = distMode ? (v) => (v / 1000).toFixed(1) : fmt;
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
        .attr("text-anchor", "end").style("fill", r.srcColor || null).text(r.label);
    });

    const gGrid = svg.append("g").attr("class", "grid");
    const gPhone = svg.append("g");
    const gR2 = svg.append("g");
    const gRows = rows.map(() => svg.append("g"));
    const gAxisX = svg.append("g").attr("class", "axis").attr("transform", `translate(0,${axisY})`);
    svg.append("clipPath").attr("id", "clip-spd").append("rect")
      .attr("x", M.l).attr("y", M.t).attr("width", width - M.l - M.r).attr("height", axisY - M.t);
    for (const g of [gPhone, gR2, ...gRows]) g.attr("clip-path", "url(#clip-spd)");

    svg.append("line").attr("class", "threshold-line")
      .attr("x1", M.l).attr("x2", width - M.r).attr("y1", y(5)).attr("y2", y(5));
    svg.append("text").attr("class", "threshold-label")
      .attr("x", width - M.r - 2).attr("y", y(5) - 3).attr("text-anchor", "end").text("5 mph");

    // Cursor: a vertical line (chart hover) + one coloured dot per displayed
    // source's speed curve. Dots + line are clipped to the plot area.
    const cursorLine = svg.append("line").attr("class", "chart-cursor")
      .attr("y1", M.t).attr("y2", axisY).style("display", "none");
    const gDots = svg.append("g").attr("clip-path", "url(#clip-spd)");

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
          .attr("height", rowH)
          .attr("fill", on ? (r.srcColor ? r.srcColor + "33" : "#fcfcfd") : "#f3f3f5") // 33 = ~20% alpha
          .attr("stroke", "#eee");
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
      if (S.cursor) renderCursor(S.cursor); // keep cursor aligned through zoom/pan
      // Coupling: push the chart's visible route-distance range to the map
      // (converting from time when in time mode). Suppressed while the map drives.
      if (S.mapState && !S._applyingMapRange) {
        const [xlo, xhi] = view.x;
        let a = distMode ? xlo : S.tToDist(xlo);
        let b = distMode ? xhi : S.tToDist(xhi);
        if (a > b) [a, b] = [b, a];
        S.mapState.publish("range:changed", { visibleDistRangeM: [a, b], source: "chart" });
      }
    }

    function drawSpeed(g, src, cls, show) {
      g.selectAll("*").remove();
      if (!src || !show) return;
      const line = d3.line().x((d) => x(d[0])).y((d) => y(d[1]));
      const pts = src.curve.t.map((tt, i) => [distMode ? src.curve.dist_m[i] : tt, src.curve.speed_mph[i]]);
      g.append("path").attr("class", `curve ${cls}`).attr("d", line(pts));
    }

    // Pixel position of the dot on a source's speed curve for a cursor spec.
    function curvePointPx(src, spec) {
      let xd, yd;
      if (spec.kind === "chart") {
        xd = spec.v;
        const arr = distMode ? src.curve.dist_m : src.curve.t;
        yd = interp(arr, src.curve.speed_mph, xd);
      } else if (distMode) {
        xd = spec.distM; yd = interp(src.curve.dist_m, src.curve.speed_mph, spec.distM);
      } else {
        xd = interp(src.curve.dist_m, src.curve.t, spec.distM); // source's time at that route distance
        yd = interp(src.curve.dist_m, src.curve.speed_mph, spec.distM);
      }
      const cx = x(xd);
      if (cx < M.l || cx > width - M.r) return null;
      return [cx, y(yd)];
    }

    function renderCursor(spec) {
      gDots.selectAll("*").remove();
      // Vertical line: chart hover (x=v), or map hover in distance mode (single x).
      let lineX = null;
      if (spec.kind === "chart") lineX = x(spec.v);
      else if (distMode) lineX = x(spec.distM);
      if (lineX != null && lineX >= M.l && lineX <= width - M.r) {
        cursorLine.attr("x1", lineX).attr("x2", lineX).style("display", null);
      } else cursorLine.style("display", "none");
      // One coloured dot per displayed source.
      for (const key of ["phone", "r2"]) {
        const on = key === "phone" ? S.toggles.phoneSpeed : S.toggles.r2Speed;
        const src = getSource(t, key);
        if (!src || !on) continue;
        const p = curvePointPx(src, spec);
        if (!p) continue;
        gDots.append("circle").attr("cx", p[0]).attr("cy", p[1]).attr("r", 4.5)
          .attr("fill", SRC_COLOR[key]).attr("stroke", "#fff").attr("stroke-width", 1.3);
      }
      self.placeBuses(spec);
    }

    S.speedCursor = {
      render: (spec) => renderCursor(spec),
      hide: () => { cursorLine.style("display", "none"); gDots.selectAll("*").remove(); },
    };

    const node = svg.node();
    const toDistM = (v) => (distMode ? v : S.tToDist(v));
    // Hover: cursor + dots + bus(es). They PERSIST after the pointer leaves so
    // you can study the map.
    svg.on("mousemove.cur", (e) => {
      const [px] = d3.pointer(e, node);
      if (px < M.l || px > width - M.r) return;
      S.cursor = { kind: "chart", v: x.invert(px) };
      renderCursor(S.cursor);
    });
    // Single click does nothing; double click → Street View at the cursor's
    // route distance (time mode uses the High-Freq trace to convert time→dist).
    svg.on("dblclick.cur", (e) => {
      const [px] = d3.pointer(e, node);
      if (px < M.l || px > width - M.r) return;
      S.mapState?.publish("streetview:open", { distM: toDistM(x.invert(px)) });
    });

    x.domain(view.x);
    installInteraction(svg,
      { x: { scale: x, full: xClamp }, y: null },
      view,
      () => ({ x: true, y: false }), // speed: horizontal only
      false, redraw);
    redraw();
    if (S.cursor) renderCursor(S.cursor); // restore persisted cursor after (re)render
  }
}
