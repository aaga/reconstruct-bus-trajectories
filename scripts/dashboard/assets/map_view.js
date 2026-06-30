// MapLibre GL wrapper. Owns the route polyline, feature markers, the
// "current position" bus icon, and emits/listens for hover + zoom state.

import maplibregl from "https://cdn.jsdelivr.net/npm/maplibre-gl@4.7.1/+esm";
import {
  projectCursorToRoute,
  visibleRouteRange,
  distToLonLat,
} from "./projection.js";
import { MAPBOX_TOKEN } from "./mapbox_token.js";

// Marker palette + sizes — keyed by feature kind. The user pinned these
// during the dashboard review: signals are the dominant "you should slow
// down" red dots, bus stops are big blue, and the less-controlling
// features (stops/crossings) are small.
const MARKER_STYLE = {
  traffic_signals:       { color: "#cc0000", radius: 7, label: "Traffic signal" },
  bus_stop:              { color: "#3a85d6", radius: 7, label: "Bus stop" },
  stop:                  { color: "#f2c543", radius: 4, label: "Stop sign" },
  ped_crossing_marked:   { color: "#00897b", radius: 4, label: "Marked crosswalk" },
  ped_crossing_signal:   { color: "#7b3fa0", radius: 4, label: "Ped signal crossing" },
};

// Minimal style. The `glyphs` URL must be defined for text-rendering layers
// (street-name labels) to load fonts. demotiles.maplibre.org is the canonical
// open glyphs server.
//
// Two basemap sources are declared up-front so toggling between them is just
// a visibility flip — no `setStyle()` call that would tear down our added
// sources and layers. The satellite layer starts hidden.
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
    // Mapbox's satellite-streets-v12 style flattened to raster tiles. The
    // /tiles/256/ endpoint serves PNGs of the full hybrid composition
    // (imagery + roads + place labels) — so toggling "Satellite" gives
    // a Google-Hybrid look in one source, no extra labels layer needed.
    "mapbox-satellite-streets": {
      type: "raster",
      tiles: [
        `https://api.mapbox.com/styles/v1/mapbox/satellite-streets-v12/tiles/256/{z}/{x}/{y}?access_token=${MAPBOX_TOKEN}`,
      ],
      tileSize: 256,
      attribution:
        '&copy; <a href="https://www.mapbox.com/about/maps/">Mapbox</a>' +
        ' &copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a>' +
        ' <strong><a href="https://www.mapbox.com/map-feedback/" target="_blank">Improve this map</a></strong>',
    },
  },
  layers: [
    { id: "carto", type: "raster", source: "carto-positron" },
    {
      id: "satellite",
      type: "raster",
      source: "mapbox-satellite-streets",
      layout: { visibility: "none" },
    },
  ],
};

export class MapView {
  constructor(container, data, state) {
    this.data = data;
    this.state = state;
    this.featuresById = new Map(data.features.map(f => [f.id, f]));
    this.hoveredFeatureId = null;
    // Each feature carries an `attributed` boolean set by the build script.
    // The "Hide features without delay" toggle then hides features with
    // attributed=false. The build script decides the criterion: for the
    // single-trip view that's "claimed by a delay band on this trip", for
    // the route aggregate view it's "p95 delay >= 0.5 min". Missing values
    // default to true (visible) so legacy payloads still render.

    const [[minLon, minLat], [maxLon, maxLat]] = data.shape.bounds;
    this.map = new maplibregl.Map({
      container,
      style: TILE_STYLE,
      bounds: [[minLon, minLat], [maxLon, maxLat]],
      bearing: data.shape.bearing_deg,
      fitBoundsOptions: {
        padding: 30,
        bearing: data.shape.bearing_deg,
      },
      attributionControl: false,
    });
    this.map.addControl(new maplibregl.AttributionControl({ compact: true }));
    this.map.addControl(new maplibregl.NavigationControl({
      visualizePitch: false, showCompass: true,
    }), "top-right");

    this.map.on("load", () => {
      if (Math.abs(this.map.getBearing() - data.shape.bearing_deg) > 0.5) {
        this.map.setBearing(data.shape.bearing_deg);
      }
      this._buildLayers();
      this._buildLegend(container);
    });
    this.map.on("move", () => this._publishRange());
    this.map.on("moveend", () => this._publishRange());

    state.subscribe("dist:hovered", ({ distM }) => this._moveCursorTo(distM));
    state.subscribe("dist:cleared", () => this._hideCursor());
    state.subscribe("feature:hovered", ({ featureId }) => this._highlightFeature(featureId));
    state.subscribe("feature:cleared", () => this._highlightFeature(null));
    // Bidirectional zoom: when the profile publishes a range, refit the map
    // to the corresponding section of the polyline. The {source: "map"}
    // tag on our own publishes lets us ignore the round-trip echo.
    state.subscribe("range:changed", (e) => {
      if (e.source !== "profile") return;
      this._fitToRange(e.visibleDistRangeM);
    });
    state.subscribe("hideUnattributed:changed", ({ value }) => {
      this._applyAttributedFilter(value);
    });
    state.subscribe("streetview:open", ({ distM }) => this._showGhost(distM));
    state.subscribe("streetview:close", () => this._hideGhost());
    state.subscribe("basemap:changed", ({ value }) => this._setBasemap(value));
  }

  _buildLayers() {
    const data = this.data;

    // -- Route polyline -------------------------------------------------
    this.map.addSource("route", {
      type: "geojson",
      data: {
        type: "Feature",
        geometry: { type: "LineString", coordinates: data.shape.polyline_lonlat },
      },
    });
    // Two-layer route line so it reads on both light (Positron) and dark
    // (satellite) basemaps without recoloring on toggle: dark halo
    // underneath, bright fill on top.
    this.map.addLayer({
      id: "route-halo",
      type: "line",
      source: "route",
      paint: {
        "line-color": "#1a1a1a",
        "line-width": 5.5,
        "line-opacity": 0.7,
      },
    });
    this.map.addLayer({
      id: "route-line",
      type: "line",
      source: "route",
      paint: {
        "line-color": "#ffcc33",
        "line-width": 3,
        "line-opacity": 0.95,
      },
    });

    // -- Feature markers ------------------------------------------------
    const fc = {
      type: "FeatureCollection",
      features: data.features.map(f => ({
        type: "Feature",
        id: f.id,
        properties: {
          fid: f.id,
          kind: f.kind,
          label: f.label,
          cross_street: f.cross_street || "",
          dist_m: f.dist_m,
          attributed: f.attributed !== false,
        },
        geometry: { type: "Point", coordinates: [f.lon, f.lat] },
      })),
    };
    this.map.addSource("features", { type: "geojson", data: fc, promoteId: "fid" });

    // Generate the maplibre `match` expressions from MARKER_STYLE so the
    // dictionary above is the single source of truth.
    const colorExpr = ["match", ["get", "kind"]];
    const baseRExpr = ["match", ["get", "kind"]];
    const hoverRExpr = ["match", ["get", "kind"]];
    for (const [kind, s] of Object.entries(MARKER_STYLE)) {
      colorExpr.push(kind, s.color);
      baseRExpr.push(kind, s.radius);
      hoverRExpr.push(kind, s.radius + 3);
    }
    colorExpr.push("#888");
    baseRExpr.push(5);
    hoverRExpr.push(8);

    this.map.addLayer({
      id: "features-fill",
      type: "circle",
      source: "features",
      paint: {
        "circle-color": colorExpr,
        "circle-radius": [
          "case",
          ["boolean", ["feature-state", "hover"], false], hoverRExpr,
          baseRExpr,
        ],
        "circle-stroke-color": "#111",
        "circle-stroke-width": [
          "case",
          ["boolean", ["feature-state", "hover"], false], 2,
          0.8,
        ],
        "circle-opacity": 0.92,
      },
    });

    // -- Cross-street labels for signals --------------------------------
    // Visible only at high zoom and always upright (text-rotation-alignment
    // = viewport) regardless of the rotated bearing. Limited to signals
    // because labeling all 220+ features would clutter the view.
    this.map.addLayer({
      id: "feature-labels",
      type: "symbol",
      source: "features",
      filter: ["any",
        ["==", ["get", "kind"], "traffic_signals"],
        ["==", ["get", "kind"], "bus_stop"],
      ],
      layout: {
        "text-field": ["get", "cross_street"],
        "text-font": ["Noto Sans Regular"],
        "text-size": 11,
        "text-anchor": "bottom",
        "text-offset": [0, -1.0],
        "text-allow-overlap": false,
        "text-ignore-placement": false,
        "text-rotation-alignment": "viewport",
        "text-pitch-alignment": "viewport",
        "symbol-sort-key": [
          "match", ["get", "kind"],
          "traffic_signals", 0,     // signals first
          "bus_stop", 1,
          2,
        ],
      },
      paint: {
        "text-color": "#222",
        "text-halo-color": "rgba(255,255,255,0.92)",
        "text-halo-width": 1.5,
      },
      minzoom: 13.5,
    });

    // -- Cursor: bus icon HTML marker -----------------------------------
    // A Marker (HTML element) rather than a layer, so we can offset it
    // straight up in screen pixels regardless of map rotation (the marker
    // sits ABOVE the polyline visually).
    const busEl = document.createElement("div");
    busEl.className = "cursor-bus";
    busEl.style.display = "none";
    busEl.innerHTML = `
      <svg viewBox="0 0 36 24" width="32" height="22" aria-hidden="true">
        <rect x="1" y="2" width="32" height="16" rx="3"
              fill="#f4b400" stroke="#222" stroke-width="1.1"/>
        <rect x="3" y="5" width="4" height="5" fill="#bce3ff" stroke="#222" stroke-width="0.7"/>
        <rect x="8.5" y="5" width="4" height="5" fill="#bce3ff" stroke="#222" stroke-width="0.7"/>
        <rect x="14" y="5" width="4" height="5" fill="#bce3ff" stroke="#222" stroke-width="0.7"/>
        <rect x="19.5" y="5" width="4" height="5" fill="#bce3ff" stroke="#222" stroke-width="0.7"/>
        <path d="M 25 4 L 32 5 L 32 11 L 25 11 Z"
              fill="#bce3ff" stroke="#222" stroke-width="0.7"/>
        <circle cx="7" cy="20" r="3" fill="#222"/>
        <circle cx="7" cy="20" r="1.3" fill="#888"/>
        <circle cx="27" cy="20" r="3" fill="#222"/>
        <circle cx="27" cy="20" r="1.3" fill="#888"/>
      </svg>`;
    this.cursorMarker = new maplibregl.Marker({
      element: busEl,
      // Stay upright on screen regardless of map rotation; sit slightly
      // above the polyline so the route line stays visible underneath.
      rotationAlignment: "viewport",
      pitchAlignment: "viewport",
      offset: [0, -16],
      anchor: "bottom",
    }).setLngLat(data.shape.polyline_lonlat[0]).addTo(this.map);
    this.cursorEl = busEl;

    // "Ghost" bus icon at the position the user clicked to open
    // Street View. Stays at that location (faded) until the popup closes.
    const ghostEl = busEl.cloneNode(true);
    ghostEl.className = "cursor-bus cursor-bus-ghost";
    ghostEl.style.display = "none";
    this.ghostMarker = new maplibregl.Marker({
      element: ghostEl,
      rotationAlignment: "viewport",
      pitchAlignment: "viewport",
      offset: [0, -16],
      anchor: "bottom",
    }).setLngLat(data.shape.polyline_lonlat[0]).addTo(this.map);
    this.ghostEl = ghostEl;

    // -- Cursor & feature hover handling --------------------------------
    this.map.on("mousemove", (e) => this._onMouseMove(e));
    this.map.on("mouseout", () => {
      this.state.publish("dist:cleared", null);
      this.state.publish("feature:cleared", null);
    });
    this.map.on("click", (e) => this._onClick(e));

    // Initial range publish (full route, since the constructor fitBounds
    // shows everything).
    this._publishRange();
  }

  _onClick(e) {
    // Project the click point onto the route and publish a streetview
    // open event. The StreetViewPopup handles the lat/lon/heading
    // computation centrally so all click sources publish the same shape.
    const cursor = [e.lngLat.lng, e.lngLat.lat];
    const proj = projectCursorToRoute(
      cursor, this.data.shape.polyline_lonlat, this.data.shape.cumdist_m
    );
    this.state.publish("streetview:open", { distM: proj.distM });
  }

  _showGhost(distM) {
    if (distM == null || !this.ghostMarker) return;
    const lonlat = distToLonLat(
      distM, this.data.shape.polyline_lonlat, this.data.shape.cumdist_m
    );
    this.ghostMarker.setLngLat(lonlat);
    this.ghostEl.style.display = "";
  }

  _hideGhost() {
    if (this.ghostEl) this.ghostEl.style.display = "none";
  }

  _onMouseMove(e) {
    const hits = this.map.queryRenderedFeatures(e.point, { layers: ["features-fill"] });
    if (hits.length > 0) {
      const fid = hits[0].properties.fid;
      if (fid !== this.hoveredFeatureId) {
        this.state.publish("feature:hovered", { featureId: fid });
      }
    } else if (this.hoveredFeatureId) {
      this.state.publish("feature:cleared", null);
    }

    const cursor = [e.lngLat.lng, e.lngLat.lat];
    const proj = projectCursorToRoute(
      cursor, this.data.shape.polyline_lonlat, this.data.shape.cumdist_m
    );
    this.state.publish("dist:hovered", { distM: proj.distM, source: "map" });
  }

  _publishRange() {
    // CRITICAL: when the profile is driving the zoom (via _fitToRange),
    // suppressPublish is set so we don't echo the map's post-fitBounds
    // visible range back. Without this check, fitBounds' padding + the
    // canvas aspect ratio would widen the range slightly, the profile
    // would receive it, snap to the wider range, and the user's zoom
    // gesture would appear to "jump back out."
    if (this._suppressPublish) return;
    if (!this.map.isStyleLoaded || !this.map.isStyleLoaded()) return;
    const [lo, hi] = visibleRouteRange(
      this.map,
      this.data.shape.polyline_lonlat,
      this.data.shape.cumdist_m,
    );
    if (this._lastLo === lo && this._lastHi === hi) return;
    this._lastLo = lo;
    this._lastHi = hi;
    this.state.publish("range:changed", { visibleDistRangeM: [lo, hi], source: "map" });
  }

  // Used when the SPEED PROFILE drives the zoom. We let MapLibre tell us
  // the section's actual on-screen extent at the current zoom (which
  // correctly accounts for bearing rotation AND any polyline curvature),
  // then bump the zoom level by log₂(desired / actual) so the section
  // fits exactly. The naïve "use straight-line endpoint distance"
  // approach undershoots whenever the polyline bows away from the chord
  // — common where Clark Street bends through Lincoln Park or the Loop.
  _fitToRange([loM, hiM]) {
    if (!this.map.isStyleLoaded || !this.map.isStyleLoaded()) return;

    const poly = this.data.shape.polyline_lonlat;
    const cum = this.data.shape.cumdist_m;
    const midM = (loM + hiM) / 2;
    const center = distToLonLat(midM, poly, cum);

    // Project every polyline vertex in the section to current-state
    // screen pixels. The (min, max) of the projected coords is the
    // section's true rendered extent — accounts for bearing rotation
    // and curvature.
    let minX = +Infinity, maxX = -Infinity, minY = +Infinity, maxY = -Infinity;
    let found = false;
    for (let i = 0; i < poly.length; i++) {
      if (cum[i] < loM || cum[i] > hiM) continue;
      const p = this.map.project(poly[i]);
      if (p.x < minX) minX = p.x;
      if (p.x > maxX) maxX = p.x;
      if (p.y < minY) minY = p.y;
      if (p.y > maxY) maxY = p.y;
      found = true;
    }
    // Also include the interpolated endpoints in case there's no polyline
    // vertex exactly at loM / hiM.
    for (const pt of [distToLonLat(loM, poly, cum), distToLonLat(hiM, poly, cum)]) {
      const p = this.map.project(pt);
      if (p.x < minX) minX = p.x;
      if (p.x > maxX) maxX = p.x;
      if (p.y < minY) minY = p.y;
      if (p.y > maxY) maxY = p.y;
      found = true;
    }
    if (!found) return;

    const sectionPxW = Math.max(1, maxX - minX);
    const sectionPxH = Math.max(1, maxY - minY);
    const canvas = this.map.getCanvas();
    const padding = 20;
    const desiredW = Math.max(1, canvas.clientWidth - 2 * padding);
    const desiredH = Math.max(1, canvas.clientHeight - 2 * padding);

    // Pick the tighter of the two so neither axis overflows.
    const factor = Math.min(desiredW / sectionPxW, desiredH / sectionPxH);
    const newZoom = Math.min(22, this.map.getZoom() + Math.log2(factor));

    // Suppress the post-jumpTo move/moveend publish so the map's
    // visible-range echo doesn't snap the profile back to a wider range.
    this._suppressPublish = true;
    const release = () => { this._suppressPublish = false; };
    const tid = setTimeout(release, 200);
    this.map.once("moveend", () => { clearTimeout(tid); release(); });
    this.map.jumpTo({
      center,
      zoom: newZoom,
      bearing: this.data.shape.bearing_deg,
    });
  }

  _moveCursorTo(distM) {
    if (distM == null || !this.cursorMarker) return;
    const lonlat = distToLonLat(
      distM, this.data.shape.polyline_lonlat, this.data.shape.cumdist_m
    );
    this.cursorMarker.setLngLat(lonlat);
    this.cursorEl.style.display = "";
  }

  _hideCursor() {
    if (this.cursorEl) this.cursorEl.style.display = "none";
  }

  // Apply / remove a layer filter that hides features whose `attributed`
  // property is false. The features GeoJSON carries that property
  // (computed in _buildLayers from this.attributedIds).
  _applyAttributedFilter(hide) {
    // Preserve the per-layer "kind" filter that limits feature-labels to
    // signals + bus stops by ANDing with our new attributed-only filter.
    const labelKindFilter = ["any",
      ["==", ["get", "kind"], "traffic_signals"],
      ["==", ["get", "kind"], "bus_stop"],
    ];
    const dotsFilter = hide ? ["boolean", ["get", "attributed"], false] : null;
    const labelsFilter = hide
      ? ["all", labelKindFilter, ["boolean", ["get", "attributed"], false]]
      : labelKindFilter;
    if (this.map.getLayer("features-fill")) {
      this.map.setFilter("features-fill", dotsFilter);
    }
    if (this.map.getLayer("feature-labels")) {
      this.map.setFilter("feature-labels", labelsFilter);
    }
  }

  _highlightFeature(featureId) {
    if (this.hoveredFeatureId === featureId) return;
    if (this.hoveredFeatureId != null) {
      this.map.setFeatureState(
        { source: "features", id: this.hoveredFeatureId },
        { hover: false },
      );
    }
    this.hoveredFeatureId = featureId;
    if (featureId != null) {
      this.map.setFeatureState(
        { source: "features", id: featureId },
        { hover: true },
      );
    }
  }

  // Tiny HTML legend in the top-left of the map pane (matches the speed
  // profile's legend in the top-right of its pane). Single source of
  // truth: MARKER_STYLE above. Also hosts the Map/Satellite basemap
  // toggle since this is where the rest of the map's chrome already lives.
  _buildLegend(container) {
    const el = document.createElement("div");
    el.className = "map-legend";
    const basemapName = `basemap-${Math.random().toString(36).slice(2, 8)}`;
    const basemapHtml = `
      <div class="legend-toggle">
        <span class="legend-toggle-label">basemap:</span>
        <label class="legend-radio">
          <input type="radio" name="${basemapName}" value="map" checked>
          <span>Map <span class="legend-shortcut">(M)</span></span>
        </label>
        <label class="legend-radio">
          <input type="radio" name="${basemapName}" value="satellite">
          <span>Satellite <span class="legend-shortcut">(S)</span></span>
        </label>
      </div>`;
    const kindRows = Object.entries(MARKER_STYLE).map(([kind, s]) => {
      const dotSize = s.radius * 2;
      return `<div class="legend-row">
        <span class="dot" style="
          width:${dotSize}px;height:${dotSize}px;background:${s.color};
        "></span>
        <span>${s.label}</span>
      </div>`;
    }).join("");
    el.innerHTML = basemapHtml + kindRows;
    container.appendChild(el);
    this._basemapRadios = el.querySelectorAll(`input[name="${basemapName}"]`);
    this._basemapRadios.forEach(input => {
      input.addEventListener("change", (event) => {
        if (event.target.checked) {
          this.state.publish("basemap:changed", { value: event.target.value });
        }
      });
    });
  }

  // Flip the visibility of the two basemap layers. The sources stay
  // mounted in the style so tiles for the inactive basemap remain cached
  // and a re-toggle is instant. Also resync the radios so M/S keyboard
  // shortcuts keep the legend in step with the actual layer state.
  _setBasemap(name) {
    const showSat = (name === "satellite");
    if (this.map.getLayer("carto")) {
      this.map.setLayoutProperty(
        "carto", "visibility", showSat ? "none" : "visible");
    }
    if (this.map.getLayer("satellite")) {
      this.map.setLayoutProperty(
        "satellite", "visibility", showSat ? "visible" : "none");
    }
    if (this._basemapRadios) {
      this._basemapRadios.forEach(r => { r.checked = (r.value === name); });
    }
  }
}
