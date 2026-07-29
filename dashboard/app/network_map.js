// Network-wide segment choropleth map.
//
// One GeoJSON source (all ~6.8k segments as LineStrings, numeric feature ids
// = sid); metric values are pushed via setFeatureState so filter changes never
// rebuild the source. Colors are computed by the caller (network_view) — this
// module is scale-agnostic: it just applies {color, width} per sid, manages
// hover/click, the legend box, and highlight/zoom for areas-of-interest.

import maplibregl from "https://cdn.jsdelivr.net/npm/maplibre-gl@4.7.1/+esm";
import { TILE_STYLE } from "./map_view.js";

const DIM_COLOR = "#d8d8d8";

export class NetworkMap {
  // onHover(feature|null, lngLat), onClick(feature|null)
  constructor(container, segmentsGeojson, { onHover, onClick } = {}) {
    this.container = container;
    this.geojson = segmentsGeojson;
    this.onHover = onHover;
    this.onClick = onClick;
    this._ready = false;
    this._pendingColors = null;
    this._highlighted = [];

    const bbox = geojsonBounds(segmentsGeojson);
    this.map = new maplibregl.Map({
      container,
      style: TILE_STYLE,
      bounds: bbox,
      fitBoundsOptions: { padding: 24 },
      attributionControl: { compact: true },
      boxZoom: false, // shift-click is corridor multi-select, not zoom-box
    });
    this.map.on("load", () => {
      this.map.addSource("segs", { type: "geojson", data: this.geojson });
      this.map.addLayer({
        id: "seg-lines",
        type: "line",
        source: "segs",
        layout: { "line-cap": "round" },
        paint: {
          "line-color": ["coalesce", ["feature-state", "c"], DIM_COLOR],
          "line-width": [
            "interpolate", ["linear"], ["zoom"],
            9, ["coalesce", ["feature-state", "w"], 1.2],
            14, ["*", 2.5, ["coalesce", ["feature-state", "w"], 1.2]],
          ],
          "line-opacity": ["coalesce", ["feature-state", "o"], 0.85],
          // Geometries are oriented in travel direction; a small rightward
          // offset renders direction pairs as parallel lanes (traffic-map
          // convention) instead of hiding one under the other, now that OSM
          // way geometry makes pairs exactly coincident on two-way streets.
          "line-offset": [
            "interpolate", ["linear"], ["zoom"],
            10, 0.6, 13, 2.0, 16, 3.5,
          ],
        },
      });
      // Direction half-arrows (harpoons): geometries are oriented in travel
      // direction, so a line-placed icon with map rotation-alignment points
      // the way the bus travels. The single barb sits on the RIGHT side —
      // the same side as the paired-direction line-offset — so a two-way
      // street reads as two opposing lanes.
      this.map.addImage("halfarrow", makeHalfArrow(22));
      this.map.addLayer({
        id: "seg-arrows",
        type: "symbol",
        source: "segs",
        minzoom: 12.5,
        layout: {
          "symbol-placement": "line",
          "symbol-spacing": 90,
          "icon-image": "halfarrow",
          "icon-size": ["interpolate", ["linear"], ["zoom"], 12.5, 0.45, 16, 0.85],
          "icon-rotation-alignment": "map",
          "icon-allow-overlap": true,
          "icon-ignore-placement": true,
          "icon-offset": [0, 5],
        },
        paint: { "icon-opacity": 0.9 },
      });

      this.map.addLayer({
        id: "seg-highlight",
        type: "line",
        source: "segs",
        layout: { "line-cap": "round" },
        paint: {
          "line-color": "#111",
          "line-width": 5,
          "line-opacity": ["case", ["boolean", ["feature-state", "hl"], false], 0.9, 0],
          "line-offset": [
            "interpolate", ["linear"], ["zoom"],
            10, 0.6, 13, 2.0, 16, 3.5,
          ],
        },
      });
      this._wireInteractions();
      this._ready = true;
      if (this._pendingColors) {
        this.setColors(this._pendingColors);
        this._pendingColors = null;
      }
    });

    this._legend = document.createElement("div");
    this._legend.className = "maplegend nw-legend";
    container.appendChild(this._legend);
    window.__nwmap = this.map; // dev/testing handle (harmless in production)
  }

  _wireInteractions() {
    let hovered = null;
    this.map.on("mousemove", (e) => {
      const feats = this.map.queryRenderedFeatures(
        [[e.point.x - 4, e.point.y - 4], [e.point.x + 4, e.point.y + 4]],
        { layers: ["seg-lines"] },
      );
      const f = feats[0] || null;
      if (hovered !== (f && f.id)) {
        hovered = f && f.id;
        this.map.getCanvas().style.cursor = f ? "pointer" : "";
      }
      this.onHover?.(f, e.lngLat, e.point);
    });
    this.map.on("mouseout", () => this.onHover?.(null));
    this.map.on("click", (e) => {
      const feats = this.map.queryRenderedFeatures(
        [[e.point.x - 5, e.point.y - 5], [e.point.x + 5, e.point.y + 5]],
        { layers: ["seg-lines"] },
      );
      this.onClick?.(feats[0] || null, e.originalEvent);
    });
  }

  // colors: Map<sid, {color, width?, opacity?}>. Sids absent from the map are
  // dimmed (grey, thin, translucent).
  setColors(colors) {
    if (!this._ready) {
      this._pendingColors = colors;
      return;
    }
    for (const f of this.geojson.features) {
      const sid = f.properties.sid;
      const c = colors.get(sid);
      this.map.setFeatureState(
        { source: "segs", id: sid },
        c
          ? { c: c.color, w: c.width ?? 2.2, o: c.opacity ?? 0.9 }
          : { c: DIM_COLOR, w: 1.0, o: 0.35 },
      );
    }
  }

  // Legend: continuous gradient with tick labels, or categorical swatches.
  setLegend({ title, gradient, ticks, note }) {
    const stops = gradient.map((c, i) => `${c} ${(100 * i) / (gradient.length - 1)}%`);
    this._legend.innerHTML = `
      <div class="nw-legend-title">${title}</div>
      <div class="nw-legend-bar" style="background:linear-gradient(to right, ${stops.join(",")})"></div>
      <div class="nw-legend-ticks">${ticks.map((t) => `<span>${t}</span>`).join("")}</div>
      ${note ? `<div class="nw-legend-note">${note}</div>` : ""}`;
  }

  highlight(sids, { zoom = true } = {}) {
    for (const sid of this._highlighted) {
      this.map.setFeatureState({ source: "segs", id: sid }, { hl: false });
    }
    this._highlighted = [...sids];
    for (const sid of this._highlighted) {
      this.map.setFeatureState({ source: "segs", id: sid }, { hl: true });
    }
    if (zoom && sids.length) {
      const set = new Set(sids);
      const feats = this.geojson.features.filter((f) => set.has(f.properties.sid));
      const bb = geojsonBounds({ features: feats });
      if (bb) this.map.fitBounds(bb, { padding: 60, maxZoom: 14.5 });
    }
  }

  setBasemap(which) {
    if (!this._ready) return;
    this.map.setLayoutProperty("carto", "visibility", which === "satellite" ? "none" : "visible");
    this.map.setLayoutProperty("satellite", "visibility", which === "satellite" ? "visible" : "none");
  }

  resize() {
    this.map.resize();
  }

  destroy() {
    this._legend.remove();
    this.map.remove();
  }
}

// A white half-arrow ("harpoon") with a dark outline, drawn on canvas so it
// stays legible over any choropleth color. Points +x; barb on top (which is
// the right-hand side once rotated along the travel direction).
function makeHalfArrow(size) {
  const c = document.createElement("canvas");
  c.width = c.height = size;
  const g = c.getContext("2d");
  const mid = size * 0.55;
  const draw = (w, color) => {
    g.strokeStyle = color;
    g.lineWidth = w;
    g.lineCap = "round";
    g.lineJoin = "round";
    g.beginPath();
    g.moveTo(size * 0.12, mid);          // stem tail
    g.lineTo(size * 0.82, mid);          // stem to tip
    g.lineTo(size * 0.38, size * 0.18);  // single barb (top = travel-right)
    g.stroke();
  };
  draw(size * 0.30, "rgba(40,40,40,0.9)");
  draw(size * 0.14, "#ffffff");
  return g.getImageData(0, 0, size, size);
}

function geojsonBounds(fc) {
  let w = Infinity, s = Infinity, e = -Infinity, n = -Infinity;
  for (const f of fc.features) {
    for (const [lon, lat] of f.geometry.coordinates) {
      if (lon < w) w = lon;
      if (lon > e) e = lon;
      if (lat < s) s = lat;
      if (lat > n) n = lat;
    }
  }
  return w === Infinity ? null : [[w, s], [e, n]];
}
