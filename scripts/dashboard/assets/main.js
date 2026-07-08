// Dashboard bootstrap. Loads data.json, instantiates the State store +
// both views, and wires them together. Add new views (e.g. a route-distance
// timeline) by importing another class and constructing it here — every
// view shares the same State pub/sub so coordination is automatic.

import { State } from "./state.js";
import { MapView } from "./map_view.js";
import { SpeedProfileView } from "./speed_profile_view.js";
import { StreetViewPopup } from "./street_view.js";
import { mountViewSwitcher } from "./view_switcher.js";

async function main() {
  const data = await fetch("./data.json").then(r => r.json());
  document.title = data.view_title || data.view_id;
  mountViewSwitcher(data);

  const state = new State();
  const profileView = new SpeedProfileView(
    document.getElementById("profile"), data, state
  );
  const mapView = new MapView(
    document.getElementById("map"), data, state
  );
  const streetView = new StreetViewPopup(data, state);

  // Global keyboard shortcuts. Bypassed when the user is typing into
  // a form field or when a modifier is held, so the shortcuts don't
  // collide with browser commands like Ctrl+D (bookmark).
  document.addEventListener("keydown", (event) => {
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    const tag = (event.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select") return;
    switch (event.key.toLowerCase()) {
      case "h":
        state.publish("hideUnattributed:changed",
          { value: !profileView.hideUnattributed });
        break;
      case "d":
        state.publish("xMode:changed", { value: "distance" });
        break;
      case "t":
        state.publish("xMode:changed", { value: "time" });
        break;
      case "m":
        state.publish("basemap:changed", { value: "map" });
        break;
      case "s":
        state.publish("basemap:changed", { value: "satellite" });
        break;
      default:
        return;
    }
    event.preventDefault();
  });

  // Hand both views a reference to the data + state, then let them drive
  // themselves. The map will publish range:changed once it loads, which
  // gives the profile its initial x-domain.
  window._dashboard = { data, state, profileView, mapView, streetView };  // for debugging
}

main().catch(err => {
  console.error(err);
  document.body.innerHTML =
    `<pre style="padding:20px;color:#c00;font-family:monospace">${err.stack || err}</pre>`;
});
