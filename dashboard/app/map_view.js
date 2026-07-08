// MapLibre GL wrapper, ported from scripts/dashboard/assets/map_view.js.
// Owns the route polyline, feature markers, the "current position" bus cursor,
// satellite toggle, and street-view click handling; syncs via the State store.
// Adapted only by adding destroy() (recreated on trip change).

import maplibregl from "https://cdn.jsdelivr.net/npm/maplibre-gl@4.7.1/+esm";
import { projectCursorToRoute, visibleRouteRange, distToLonLat } from "./projection.js";
// Mapbox public token, vendored into this folder so the bundle is
// self-contained when deployed standalone (Cloudflare Pages, the VM, or a
// plain static server). Kept in sync with scripts/dashboard/assets/mapbox_token.js
// until the two dashboards are merged.
import { MAPBOX_TOKEN } from "./mapbox_token.js";

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

    // The trajectory bus markers are owned by main.js; MapView keeps the basemap
    // wiring + the bidirectional zoom coupling (range:changed) shared with the
    // chart above it.
    this._unsub = [
      state.subscribe("basemap:changed", ({ value }) => this._setBasemap(value)),
      state.subscribe("range:changed", (e) => { if (e.source !== "map") this._fitToRange(e.visibleDistRangeM); }),
      state.subscribe("hideUnattributed:changed", ({ value }) => this._setHideUnattributed(value)),
    ];
    // Aggregate (delay-per-segment) view has no bus markers, so the map shows a
    // cursor dot that the DelayView drives via dist:hovered.
    if (data.kind === "aggregate") {
      this._cursorEl = document.createElement("div");
      this._cursorEl.className = "map-cursor-dot";
      this._cursorEl.style.display = "none";
      this._cursor = new maplibregl.Marker({ element: this._cursorEl, anchor: "center" })
        .setLngLat(data.shape.polyline_lonlat[0]).addTo(this.map);
      this._unsub.push(
        state.subscribe("dist:hovered", (e) => this._showCursor(e.distM)),
        state.subscribe("dist:cleared", () => { this._cursorEl.style.display = "none"; }),
      );
    }
  }

  _showCursor(distM) {
    if (!this._cursor || distM == null || Number.isNaN(distM)) {
      if (this._cursorEl) this._cursorEl.style.display = "none";
      return;
    }
    this._cursor.setLngLat(distToLonLat(distM, this.data.shape.polyline_lonlat, this.data.shape.cumdist_m));
    this._cursorEl.style.display = "";
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
        properties: { fid: f.id, kind: f.kind, label: f.label, cross_street: f.cross_street || "", dist_m: f.dist_m, attributed: !!f.attributed },
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
    // Bidirectional zoom coupling: map pan/zoom → publish the visible route
    // distance range so the chart above can follow.
    this.map.on("move", () => this._publishRange());
    this.map.on("moveend", () => this._publishRange());
  }

  _publishRange() {
    // Suppressed while the chart is driving the zoom (_fitToRange), so the
    // map's post-jumpTo visible range doesn't echo back and widen the chart.
    if (this._suppressPublish) return;
    if (!this.map.isStyleLoaded || !this.map.isStyleLoaded()) return;
    const [lo, hi] = visibleRouteRange(this.map, this.data.shape.polyline_lonlat, this.data.shape.cumdist_m);
    if (this._lastLo === lo && this._lastHi === hi) return;
    this._lastLo = lo; this._lastHi = hi;
    this.state.publish("range:changed", { visibleDistRangeM: [lo, hi], source: "map" });
  }

  // Chart drives the zoom: fit the route section [loM,hiM] on screen. We let
  // MapLibre report the section's true rendered pixel extent at the current
  // zoom (accounts for bearing + polyline curvature), then bump the zoom by
  // log2(desired/actual) so it fits exactly.
  _fitToRange([loM, hiM]) {
    if (!this.map.isStyleLoaded || !this.map.isStyleLoaded()) return;
    const poly = this.data.shape.polyline_lonlat;
    const cum = this.data.shape.cumdist_m;
    const center = distToLonLat((loM + hiM) / 2, poly, cum);
    let minX = +Infinity, maxX = -Infinity, minY = +Infinity, maxY = -Infinity, found = false;
    for (let i = 0; i < poly.length; i++) {
      if (cum[i] < loM || cum[i] > hiM) continue;
      const p = this.map.project(poly[i]);
      if (p.x < minX) minX = p.x; if (p.x > maxX) maxX = p.x;
      if (p.y < minY) minY = p.y; if (p.y > maxY) maxY = p.y;
      found = true;
    }
    for (const pt of [distToLonLat(loM, poly, cum), distToLonLat(hiM, poly, cum)]) {
      const p = this.map.project(pt);
      if (p.x < minX) minX = p.x; if (p.x > maxX) maxX = p.x;
      if (p.y < minY) minY = p.y; if (p.y > maxY) maxY = p.y;
      found = true;
    }
    if (!found) return;
    const canvas = this.map.getCanvas();
    const padding = 20;
    const factor = Math.min(
      Math.max(1, canvas.clientWidth - 2 * padding) / Math.max(1, maxX - minX),
      Math.max(1, canvas.clientHeight - 2 * padding) / Math.max(1, maxY - minY),
    );
    const newZoom = Math.min(22, this.map.getZoom() + Math.log2(factor));
    this._suppressPublish = true;
    const release = () => { this._suppressPublish = false; };
    const tid = setTimeout(release, 200);
    this.map.once("moveend", () => { clearTimeout(tid); release(); });
    this.map.jumpTo({ center, zoom: newZoom, bearing: this.data.shape.bearing_deg });
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
    const hideId = `maphide-${Math.random().toString(36).slice(2, 8)}`;
    const hideHtml = `
      <div class="legend-toggle">
        <input id="${hideId}" type="checkbox">
        <label for="${hideId}">Hide features without delay <span class="legend-shortcut">(H)</span></label>
      </div>`;
    const kindRows = Object.entries(MARKER_STYLE).map(([kind, s]) => {
      const d = s.radius * 2;
      return `<div class="legend-row"><span class="dot" style="width:${d}px;height:${d}px;background:${s.color};"></span><span>${s.label}</span></div>`;
    }).join("");
    el.innerHTML = basemapHtml + hideHtml + kindRows;
    container.appendChild(el);
    this._basemapRadios = el.querySelectorAll(`input[name="${name}"]`);
    this._basemapRadios.forEach((input) =>
      input.addEventListener("change", (e) => {
        if (e.target.checked) this.state.publish("basemap:changed", { value: e.target.value });
      }));
    this._hideCheckbox = el.querySelector(`#${hideId}`);
    this._hideCheckbox.addEventListener("change", (e) =>
      this.state.publish("hideUnattributed:changed", { value: e.target.checked }));
  }

  // Hide/show map features with no attributed delay (drives both the fill dots
  // and their labels; keeps the checkbox in sync when toggled via the H key).
  _setHideUnattributed(hide) {
    const attr = ["==", ["get", "attributed"], true];
    if (this.map.getLayer("features-fill")) this.map.setFilter("features-fill", hide ? attr : null);
    if (this.map.getLayer("feature-labels")) {
      const base = ["any", ["==", ["get", "kind"], "traffic_signals"], ["==", ["get", "kind"], "bus_stop"]];
      this.map.setFilter("feature-labels", hide ? ["all", base, attr] : base);
    }
    if (this._hideCheckbox) this._hideCheckbox.checked = hide;
  }

  _setBasemap(name) {
    const showSat = name === "satellite";
    if (this.map.getLayer("carto")) this.map.setLayoutProperty("carto", "visibility", showSat ? "none" : "visible");
    if (this.map.getLayer("satellite")) this.map.setLayoutProperty("satellite", "visibility", showSat ? "visible" : "none");
    if (this._basemapRadios) this._basemapRadios.forEach((r) => { r.checked = r.value === name; });
  }
}
