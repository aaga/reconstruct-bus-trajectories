// Trajectory tab: distance-along-route vs Chicago-local time, with phone(bw20)
// + R2(bw5) smoothed curves, optional raw pings, optional stop lines.
// Wheel zooms both axes (SHIFT = vertical only, CMD/CTRL = horizontal only);
// drag pans. Body is the original renderTrajectory(), keyed off the shared S.

import * as d3 from "https://cdn.jsdelivr.net/npm/d3@7.9.0/+esm";
import {
  makeSvg, installInteraction, fmtClock, defaultXExtent, defaultYExtentKm, timeExtent, getSource,
} from "../chart_util.js";

export class TrajectoryView {
  constructor(S) { this.S = S; }

  render() {
    const S = this.S;
    const t = S.trip;
    const fmt = (tSec) => fmtClock(t, tSec);
    const M = { l: 56, r: 16, t: 14, b: 38 };
    const { svg, width, height } = makeSvg();

    const xDefault = defaultXExtent(S);
    const yDefault = defaultYExtentKm(S);
    // Default view crops to the primary window; the pan/zoom CLAMP is the whole
    // trip (both sources), so you can scroll into the low-freq-only region.
    const xClamp = timeExtent(t);
    let maxD = 0; for (const s of t.sources) maxD = Math.max(maxD, d3.max(s.curve.dist_m));
    const yClamp = [0, maxD / 1000];
    const view = S.view.trajectory || (S.view.trajectory = { x: xDefault.slice(), y: yDefault.slice() });

    const x = d3.scaleLinear().range([M.l, width - M.r]);
    const y = d3.scaleLinear().range([height - M.b, M.t]);

    svg.append("text").attr("transform", "rotate(-90)").attr("x", -(height / 2)).attr("y", 14)
      .attr("text-anchor", "middle").attr("font-size", 12).attr("fill", "#555")
      .text("distance along route (km)");

    const gGrid = svg.append("g").attr("class", "grid");
    const gAxisY = svg.append("g").attr("class", "axis").attr("transform", `translate(${M.l},0)`);
    const gStops = svg.append("g");
    const gPhone = svg.append("g");
    const gR2 = svg.append("g");
    const gAxisX = svg.append("g").attr("class", "axis").attr("transform", `translate(0,${height - M.b})`);
    // clip so panned content doesn't spill over the axes
    svg.append("clipPath").attr("id", "clip-traj").append("rect")
      .attr("x", M.l).attr("y", M.t).attr("width", width - M.l - M.r).attr("height", height - M.t - M.b);
    for (const g of [gStops, gPhone, gR2]) g.attr("clip-path", "url(#clip-traj)");

    function redraw() {
      x.domain(view.x); y.domain(view.y);
      gAxisX.call(d3.axisBottom(x).ticks(8).tickFormat(fmt));
      gAxisY.call(d3.axisLeft(y).ticks(8));
      gGrid.attr("transform", `translate(0,${height - M.b})`)
        .call(d3.axisBottom(x).ticks(8).tickSize(-(height - M.t - M.b)).tickFormat(""));
      gGrid.selectAll(".domain").remove();

      gStops.selectAll("line").remove();
      if (S.toggles.stops) {
        gStops.selectAll("line").data(t.features.filter((f) => f.kind === "bus_stop"))
          .join("line").attr("class", "stopline")
          .attr("x1", M.l).attr("x2", width - M.r)
          .attr("y1", (d) => y(d.dist_m / 1000)).attr("y2", (d) => y(d.dist_m / 1000));
      }
      drawSource(gPhone, getSource(t, "phone"), "phone", S.toggles.phoneCurve, S.toggles.phoneRaw);
      drawSource(gR2, getSource(t, "r2"), "r2", S.toggles.r2Curve, S.toggles.r2Raw);
    }

    function drawSource(g, src, cls, showCurve, showRaw) {
      g.selectAll("*").remove();
      if (!src) return;
      if (showRaw) {
        g.selectAll("circle").data(src.raw_pings).join("circle").attr("class", `raw ${cls}`)
          .attr("cx", (d) => x(d.t)).attr("cy", (d) => y(d.dist_m / 1000)).attr("r", 2).attr("opacity", 0.45);
      }
      if (showCurve) {
        const line = d3.line().x((d) => x(d[0])).y((d) => y(d[1]));
        const pts = src.curve.t.map((tt, i) => [tt, src.curve.dist_m[i] / 1000]);
        g.append("path").attr("class", `curve ${cls}`).attr("d", line(pts));
      }
    }

    x.domain(view.x); y.domain(view.y);
    installInteraction(svg,
      { x: { scale: x, full: xClamp }, y: { scale: y, full: yClamp } },
      view,
      (e) => ({ x: !e.shiftKey, y: !(e.metaKey || e.ctrlKey) }), // shift=y, cmd=x, none=both
      true, redraw);
    redraw();
  }
}
