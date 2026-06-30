// MapLibre GL wrapper, ported from scripts/dashboard/assets/map_view.js.
// Owns the route polyline, feature markers, the "current position" bus cursor,
// satellite toggle, and street-view click handling; syncs via the State store.
// Adapted only by adding destroy() (recreated on trip change).

import maplibregl from "https://cdn.jsdelivr.net/npm/maplibre-gl@4.7.1/+esm";
import { projectCursorToRoute, visibleRouteRange, distToLonLat } from "./projection.js";
// Single source of truth for the token, in the parent dashboard's assets.
// (This analysis dashboard is a repo-local dev viewer served from the repo
// root, so the relative hop is safe; it collapses fully when the two
// dashboards are merged.)
import { MAPBOX_TOKEN } from "../../../scripts/dashboard/assets/mapbox_token.js";

const MARKER_STYLE = {
  traffic_signals:       { color: "#cc0000", radius: 7, label: "Traffic signal" },
  bus_stop:              { color: "#3a85d6", radius: 7, label: "Bus stop" },
  stop:                  { color: "#f2c543", radius: 4, label: "Stop sign" },
  ped_crossing_marked:   { color: "#00897b", radius: 4, label: "Marked crosswalk" },
  ped_crossing_signal:   { color: "#7b3fa0", radius: 4, label: "Ped signal crossing" },
};

const TILE_STYLE = {
  version: 8,
  glyphs: "https://demotiles.maplibre.org/font/{fontstack}/{range}.pbf",
  sources: {
    "carto-positron": {
      type: "raster",
      tiles: [
        "https://a.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
        "https://b.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
        "https://c.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
      ],
      tileSize: 256,
      attribution:
        '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>' +
        ' contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
    },
    "mapbox-satellite-streets": {
      type: "raster",
      tiles: [
        `https://api.mapbox.com/styles/v1/mapbox/satellite-streets-v12/tiles/256/{z}/{x}/{y}?access_token=${MAPBOX_TOKEN}`,
      ],
      tileSize: 256,
      attribution:
        '&copy; <a href="https://www.mapbox.com/about/maps/">Mapbox</a>' +
        ' &copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>',
    },
  },
  layers: [
    { id: "carto", type: "raster", source: "carto-positron" },
    { id: "satellite", type: "raster", source: "mapbox-satellite-streets", layout: { visibility: "none" } },
  ],
};

export class MapView {
  constructor(container, data, state) {
    this.data = data;
    this.state = state;
    this.container = container;
    this.hoveredFeatureId = null;

    const [[minLon, minLat], [maxLon, maxLat]] = data.shape.bounds;
    this.map = new maplibregl.Map({
      container,
      style: TILE_STYLE,
      bounds: [[minLon, minLat], [maxLon, maxLat]],
      bearing: data.shape.bearing_deg,
      fitBoundsOptions: { padding: 24, bearing: data.shape.bearing_deg },
      attributionControl: false,
    });
    this.map.addControl(new maplibregl.AttributionControl({ compact: true }));
    this.map.addControl(new maplibregl.NavigationControl({ visualizePitch: false, showCompass: true }), "top-right");
    // Double-click is repurposed for Street View, so disable its default zoom.
    this.map.doubleClickZoom.disable();

    this.map.on("load", () => {
      if (Math.abs(this.map.getBearing() - data.shape.bearing_deg) > 0.5) {
        this.map.setBearing(data.shape.bearing_deg);
      }
      this._buildLayers();
      this._buildLegend(container);
    });

    // The trajectory bus markers are owned by main.js; MapView keeps only the
    // basemap wiring (no street-view ghost any more).
    this._unsub = [
      state.subscribe("basemap:changed", ({ value }) => this._setBasemap(value)),
    ];
  }

  destroy() {
    this._unsub?.forEach((u) => u());
    this.map.remove();
    this.container.querySelector(".map-legend")?.remove();
  }

  resize() { this.map.resize(); }

  _buildLayers() {
    const data = this.data;
    this.map.addSource("route", {
      type: "geojson",
      data: { type: "Feature", geometry: { type: "LineString", coordinates: data.shape.polyline_lonlat } },
    });
    this.map.addLayer({ id: "route-halo", type: "line", source: "route",
      paint: { "line-color": "#1a1a1a", "line-width": 5.5, "line-opacity": 0.7 } });
    this.map.addLayer({ id: "route-line", type: "line", source: "route",
      paint: { "line-color": "#ffcc33", "line-width": 3, "line-opacity": 0.95 } });

    const fc = {
      type: "FeatureCollection",
      features: data.features.map((f) => ({
        type: "Feature", id: f.id,
        properties: { fid: f.id, kind: f.kind, label: f.label, cross_street: f.cross_street || "", dist_m: f.dist_m },
        geometry: { type: "Point", coordinates: [f.lon, f.lat] },
      })),
    };
    this.map.addSource("features", { type: "geojson", data: fc, promoteId: "fid" });

    const colorExpr = ["match", ["get", "kind"]];
    const baseRExpr = ["match", ["get", "kind"]];
    const hoverRExpr = ["match", ["get", "kind"]];
    for (const [kind, s] of Object.entries(MARKER_STYLE)) {
      colorExpr.push(kind, s.color); baseRExpr.push(kind, s.radius); hoverRExpr.push(kind, s.radius + 3);
    }
    colorExpr.push("#888"); baseRExpr.push(5); hoverRExpr.push(8);

    this.map.addLayer({
      id: "features-fill", type: "circle", source: "features",
      paint: {
        "circle-color": colorExpr,
        "circle-radius": ["case", ["boolean", ["feature-state", "hover"], false], hoverRExpr, baseRExpr],
        "circle-stroke-color": "#111",
        "circle-stroke-width": ["case", ["boolean", ["feature-state", "hover"], false], 2, 0.8],
        "circle-opacity": 0.92,
      },
    });

    this.map.addLayer({
      id: "feature-labels", type: "symbol", source: "features",
      filter: ["any", ["==", ["get", "kind"], "traffic_signals"], ["==", ["get", "kind"], "bus_stop"]],
      layout: {
        "text-field": ["get", "cross_street"], "text-font": ["Noto Sans Regular"], "text-size": 11,
        "text-anchor": "bottom", "text-offset": [0, -1.0], "text-allow-overlap": false,
        "text-rotation-alignment": "viewport", "text-pitch-alignment": "viewport",
        "symbol-sort-key": ["match", ["get", "kind"], "traffic_signals", 0, "bus_stop", 1, 2],
      },
      paint: { "text-color": "#222", "text-halo-color": "rgba(255,255,255,0.92)", "text-halo-width": 1.5 },
      minzoom: 13.5,
    });

    this.map.on("mousemove", (e) => this._onMouseMove(e));
    this.map.on("mouseout", () => {
      this.state.publish("dist:cleared", null);
      this.state.publish("feature:cleared", null);
    });
    // Single click does nothing; double click opens Street View.
    this.map.on("dblclick", (e) => this._onDblClick(e));
  }

  _onDblClick(e) {
    const proj = projectCursorToRoute([e.lngLat.lng, e.lngLat.lat], this.data.shape.polyline_lonlat, this.data.shape.cumdist_m);
    this.state.publish("streetview:open", { distM: proj.distM });
  }

  _onMouseMove(e) {
    const hits = this.map.queryRenderedFeatures(e.point, { layers: ["features-fill"] });
    if (hits.length > 0) {
      const fid = hits[0].properties.fid;
      if (fid !== this.hoveredFeatureId) this._highlightFeature(fid);
    } else if (this.hoveredFeatureId) {
      this._highlightFeature(null);
    }
    const proj = projectCursorToRoute([e.lngLat.lng, e.lngLat.lat], this.data.shape.polyline_lonlat, this.data.shape.cumdist_m);
    this.state.publish("dist:hovered", { distM: proj.distM, source: "map" });
  }

  _highlightFeature(featureId) {
    if (this.hoveredFeatureId === featureId) return;
    if (this.hoveredFeatureId != null) {
      this.map.setFeatureState({ source: "features", id: this.hoveredFeatureId }, { hover: false });
    }
    this.hoveredFeatureId = featureId;
    if (featureId != null) {
      this.map.setFeatureState({ source: "features", id: featureId }, { hover: true });
    }
  }

  _buildLegend(container) {
    const el = document.createElement("div");
    el.className = "map-legend";
    const name = `basemap-${Math.random().toString(36).slice(2, 8)}`;
    const basemapHtml = `
      <div class="legend-toggle">
        <span class="legend-toggle-label">basemap:</span>
        <label class="legend-radio"><input type="radio" name="${name}" value="map" checked>
          <span>Map <span class="legend-shortcut">(M)</span></span></label>
        <label class="legend-radio"><input type="radio" name="${name}" value="satellite">
          <span>Satellite <span class="legend-shortcut">(S)</span></span></label>
      </div>`;
    const kindRows = Object.entries(MARKER_STYLE).map(([kind, s]) => {
      const d = s.radius * 2;
      return `<div class="legend-row"><span class="dot" style="width:${d}px;height:${d}px;background:${s.color};"></span><span>${s.label}</span></div>`;
    }).join("");
    el.innerHTML = basemapHtml + kindRows;
    container.appendChild(el);
    this._basemapRadios = el.querySelectorAll(`input[name="${name}"]`);
    this._basemapRadios.forEach((input) =>
      input.addEventListener("change", (e) => {
        if (e.target.checked) this.state.publish("basemap:changed", { value: e.target.value });
      }));
  }

  _setBasemap(name) {
    const showSat = name === "satellite";
    if (this.map.getLayer("carto")) this.map.setLayoutProperty("carto", "visibility", showSat ? "none" : "visible");
    if (this.map.getLayer("satellite")) this.map.setLayoutProperty("satellite", "visibility", showSat ? "visible" : "none");
    if (this._basemapRadios) this._basemapRadios.forEach((r) => { r.checked = r.value === name; });
  }
}
