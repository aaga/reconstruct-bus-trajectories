// Average-trip "Overall delay" tab: an F3-style breakdown of mean minutes per
// trip by category, computed on-the-fly from the aggregate's segments[]. Full
// F3 = free-flow + every delay category.

import * as d3 from "https://cdn.jsdelivr.net/npm/d3@7.9.0/+esm";
import { makeSvg } from "../chart_util.js";

// Category → the segment minute-fields to sum (neg fields subtract). Colours
// match the DelayView (Delay-per-segment) stack.
const CATS = [
  { label: "Free-flow", color: "#9bd4a2", fields: ["t_ff_min"] },
  { label: "Dwell", color: "#3a85d6", fields: ["t_dwell_clean_min", "t_dwell_near_signal_min"] },
  { label: "Signal", color: "#cc0000", fields: ["d_signal_uniform_min", "d_signal_overflow_min"] },
  { label: "Crossing", color: "#d6b56a", fields: ["d_crossing_min"] },
  { label: "Congestion", color: "#b27ab2", fields: ["d_congestion_pos_min"], neg: ["d_congestion_neg_min"] },
];

export class OverallDelayView {
  constructor(S) { this.S = S; }

  render() {
    const agg = this.S.agg;
    const totals = CATS.map((c) => {
      let v = 0;
      for (const seg of agg.segments) {
        for (const f of c.fields) v += seg[f] || 0;
        for (const f of c.neg || []) v -= seg[f] || 0;
      }
      return { ...c, value: v };
    });

    const { svg, width, height } = makeSvg();
    const M = { l: 64, r: 20, t: 46, b: 54 };
    const x = d3.scaleBand().domain(totals.map((d) => d.label)).range([M.l, width - M.r]).padding(0.32);
    const y = d3.scaleLinear().domain([Math.min(0, d3.min(totals, (d) => d.value)),
      (d3.max(totals, (d) => d.value) || 1) * 1.12]).nice().range([height - M.b, M.t]);

    svg.append("text").attr("x", M.l).attr("y", 24).attr("font-size", 15).attr("font-weight", 600)
      .attr("fill", "#222").text(`Mean delay per trip by category`);
    svg.append("text").attr("x", M.l).attr("y", 40).attr("font-size", 12).attr("fill", "#777")
      .text(`${agg.label} — ${agg.n_trips} trips`);

    svg.append("g").attr("transform", `translate(0,${y(0)})`).call(d3.axisBottom(x))
      .selectAll("text").attr("font-size", 12).attr("fill", "#333");
    svg.append("g").attr("transform", `translate(${M.l},0)`).call(d3.axisLeft(y).ticks(6));
    svg.append("text").attr("transform", "rotate(-90)").attr("x", -(height / 2)).attr("y", 16)
      .attr("text-anchor", "middle").attr("fill", "#555").attr("font-size", 12).text("minutes / trip (mean)");

    svg.selectAll("rect.cat").data(totals).join("rect").attr("class", "cat")
      .attr("x", (d) => x(d.label)).attr("width", x.bandwidth())
      .attr("y", (d) => y(Math.max(0, d.value)))
      .attr("height", (d) => Math.abs(y(d.value) - y(0)))
      .attr("fill", (d) => d.color).attr("opacity", 0.9);

    svg.selectAll("text.val").data(totals).join("text").attr("class", "val")
      .attr("x", (d) => x(d.label) + x.bandwidth() / 2)
      .attr("y", (d) => y(Math.max(0, d.value)) - 6)
      .attr("text-anchor", "middle").attr("font-size", 12).attr("font-weight", 600).attr("fill", "#333")
      .text((d) => d.value.toFixed(2));
  }
}
