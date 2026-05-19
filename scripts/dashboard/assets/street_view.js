// Draggable Street-View popup. Listens for `streetview:open` events
// (published by the map and speed-profile click handlers), resolves the
// distance to a lat/lon + heading using the same polyline + cumdist as
// the rest of the dashboard, and points an embedded Google Street View
// iframe at that location, oriented along the direction of travel.
//
// The popup:
//   - is draggable by its title bar
//   - has an "×" button to close
//   - dismisses on Esc
//   - reloads in place when another click fires while it's open
//
// Closing the popup publishes `streetview:close` so the ghost markers
// in the other views can clear themselves.

import { distToLonLat, headingAtDistDeg } from "./projection.js";

// Unauthenticated Google Street View embed URL. `cbll` = camera lat/lon,
// `cbp` = zoom,heading,tilt,pitch,roll (heading in compass degrees with
// 0 = north, 180 = south). The `output=svembed` query parameter is what
// makes Google return a Street View iframe instead of the full Maps UI.
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

    state.subscribe("streetview:open", ({ distM }) => this.open(distM));
    state.subscribe("streetview:close", () => this.close());
  }

  // ----- public ------------------------------------------------------

  open(distM) {
    if (distM == null) return;
    const ll = distToLonLat(
      distM, this.data.shape.polyline_lonlat, this.data.shape.cumdist_m
    );
    const heading = headingAtDistDeg(
      distM, this.data.shape.polyline_lonlat, this.data.shape.cumdist_m
    );
    const url = streetViewURL(ll[1], ll[0], heading);
    this.iframe.src = url;
    this.el.style.display = "flex";
    this.currentDistM = distM;
    this._updateTitle(distM, ll[1], ll[0]);
  }

  close() {
    this.el.style.display = "none";
    this.currentDistM = null;
    // Clear the iframe src so the Google player stops loading panos in
    // the background.
    this.iframe.src = "about:blank";
  }

  isOpen() {
    return this.el.style.display !== "none";
  }

  // ----- internals ---------------------------------------------------

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
    // Default to bottom-right corner, sized at roughly 1/3 of the window
    // width with a 4:3 aspect ratio.
    const margin = 20;
    const w = Math.max(380, Math.round(window.innerWidth / 3));
    const h = Math.round(w * 0.75);
    const x = Math.max(margin, window.innerWidth - w - margin);
    const y = Math.max(margin, window.innerHeight - h - margin);
    this.el.style.width = `${w}px`;
    this.el.style.height = `${h}px`;
    this.el.style.left = `${x}px`;
    this.el.style.top = `${y}px`;
  }

  _wireDrag() {
    // Drag via mousedown on the header (anywhere outside the close
    // button). The transparent overlay (.streetview-drag-overlay)
    // covers the iframe ONLY while dragging, so the iframe doesn't
    // swallow the mousemove/mouseup events that fly across it.
    let drag = null;
    this.header.addEventListener("mousedown", (e) => {
      if (e.target === this.closeBtn) return;
      const rect = this.el.getBoundingClientRect();
      drag = {
        offsetX: e.clientX - rect.left,
        offsetY: e.clientY - rect.top,
      };
      document.body.classList.add("streetview-dragging");
      e.preventDefault();
    });
    document.addEventListener("mousemove", (e) => {
      if (!drag) return;
      // Clamp so the popup stays inside the visible window.
      const w = this.el.offsetWidth;
      const h = this.el.offsetHeight;
      const x = Math.max(0, Math.min(window.innerWidth - w, e.clientX - drag.offsetX));
      const y = Math.max(0, Math.min(window.innerHeight - h, e.clientY - drag.offsetY));
      this.el.style.left = `${x}px`;
      this.el.style.top = `${y}px`;
    });
    document.addEventListener("mouseup", () => {
      drag = null;
      document.body.classList.remove("streetview-dragging");
    });
  }

  _wireClose() {
    this.closeBtn.addEventListener("click", () =>
      this.state.publish("streetview:close", null));
  }

  _wireKeyboard() {
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && this.isOpen()) {
        this.state.publish("streetview:close", null);
      }
    });
  }

  _wireFocusRecapture() {
    // Once the user clicks inside the cross-origin Google Maps iframe,
    // it takes focus and keypresses no longer reach the parent
    // document — so Esc (and the D/T/H shortcuts) stop firing. Recover
    // by handing focus back to the parent the moment the cursor leaves
    // the iframe; the user only has to mouse-out before pressing Esc.
    //
    // We don't blur the iframe while the cursor is over it (that would
    // break Street View's own pan/zoom). Mouseleave is the moment the
    // user is "done" with that pan, so it's a natural place to recover
    // focus.
    this.iframe.addEventListener("mouseleave", () => {
      if (document.activeElement === this.iframe) {
        this.iframe.blur();
        window.focus();
      }
    });
  }

  _updateTitle(distM, lat, lon) {
    const mi = (distM / 1609.344).toFixed(2);
    this.title.textContent = `Street View · ${mi} mi · ${lat.toFixed(5)}, ${lon.toFixed(5)}`;
  }
}
