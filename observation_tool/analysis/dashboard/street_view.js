// Draggable Street-View popup, ported verbatim from
// scripts/dashboard/assets/street_view.js. Listens for `streetview:open`
// (published by the map and the speed-chart click handler), resolves the
// distance to lat/lon + heading via the shared polyline, and points an
// embedded Google Street View iframe there along the direction of travel.

import { distToLonLat, headingAtDistDeg } from "./projection.js";

function streetViewURL(lat, lon, heading) {
  const h = ((Math.round(heading) % 360) + 360) % 360;
  return `https://www.google.com/maps?q=&layer=c&cbll=${lat.toFixed(6)},${lon.toFixed(6)}` +
         `&cbp=11,${h},0,0,0&output=svembed`;
}

export class StreetViewPopup {
  constructor(data, state) {
    this.data = data;
    this.state = state;
    this.currentDistM = null;

    this.el = this._buildElement();
    document.body.appendChild(this.el);
    this.el.style.display = "none";

    this._initialPosition();
    this._wireDrag();
    this._wireClose();
    this._wireKeyboard();
    this._wireFocusRecapture();

    this._unsub = [
      state.subscribe("streetview:open", ({ distM }) => this.open(distM)),
      state.subscribe("streetview:close", () => this.close()),
    ];
  }

  open(distM) {
    if (distM == null) return;
    const ll = distToLonLat(distM, this.data.shape.polyline_lonlat, this.data.shape.cumdist_m);
    const heading = headingAtDistDeg(distM, this.data.shape.polyline_lonlat, this.data.shape.cumdist_m);
    this.iframe.src = streetViewURL(ll[1], ll[0], heading);
    this.el.style.display = "flex";
    this.currentDistM = distM;
    this._updateTitle(distM, ll[1], ll[0]);
  }

  close() {
    this.el.style.display = "none";
    this.currentDistM = null;
    this.iframe.src = "about:blank";
  }

  isOpen() { return this.el.style.display !== "none"; }

  // Remove the popup + its document-level listeners (on trip change).
  destroy() {
    this._unsub?.forEach((u) => u());
    this.el.remove();
  }

  _buildElement() {
    const popup = document.createElement("div");
    popup.className = "streetview-popup";
    popup.innerHTML = `
      <div class="streetview-header">
        <span class="streetview-title">Street View</span>
        <button class="streetview-close" type="button" aria-label="Close">
          <span class="streetview-close-hint">(Esc)</span>×
        </button>
      </div>
      <iframe class="streetview-iframe" frameborder="0"
              referrerpolicy="no-referrer-when-downgrade"
              allow="fullscreen"></iframe>
      <div class="streetview-drag-overlay"></div>
    `;
    this.iframe = popup.querySelector(".streetview-iframe");
    this.title = popup.querySelector(".streetview-title");
    this.header = popup.querySelector(".streetview-header");
    this.closeBtn = popup.querySelector(".streetview-close");
    return popup;
  }

  _initialPosition() {
    const margin = 20;
    const w = Math.max(380, Math.round(window.innerWidth / 3));
    const h = Math.round(w * 0.75);
    this.el.style.width = `${w}px`;
    this.el.style.height = `${h}px`;
    this.el.style.left = `${Math.max(margin, window.innerWidth - w - margin)}px`;
    this.el.style.top = `${Math.max(margin, window.innerHeight - h - margin)}px`;
  }

  _wireDrag() {
    let drag = null;
    this.header.addEventListener("mousedown", (e) => {
      if (e.target === this.closeBtn) return;
      const rect = this.el.getBoundingClientRect();
      drag = { offsetX: e.clientX - rect.left, offsetY: e.clientY - rect.top };
      document.body.classList.add("streetview-dragging");
      e.preventDefault();
    });
    document.addEventListener("mousemove", (e) => {
      if (!drag) return;
      const w = this.el.offsetWidth, h = this.el.offsetHeight;
      this.el.style.left = `${Math.max(0, Math.min(window.innerWidth - w, e.clientX - drag.offsetX))}px`;
      this.el.style.top = `${Math.max(0, Math.min(window.innerHeight - h, e.clientY - drag.offsetY))}px`;
    });
    document.addEventListener("mouseup", () => {
      drag = null;
      document.body.classList.remove("streetview-dragging");
    });
  }

  _wireClose() {
    this.closeBtn.addEventListener("click", () => this.state.publish("streetview:close", null));
  }

  _wireKeyboard() {
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && this.isOpen()) this.state.publish("streetview:close", null);
    });
  }

  _wireFocusRecapture() {
    this.iframe.addEventListener("mouseleave", () => {
      if (document.activeElement === this.iframe) { this.iframe.blur(); window.focus(); }
    });
  }

  _updateTitle(distM, lat, lon) {
    const mi = (distM / 1609.344).toFixed(2);
    this.title.textContent = `Street View · ${mi} mi · ${lat.toFixed(5)}, ${lon.toFixed(5)}`;
  }
}
