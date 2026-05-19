// Route-aggregate dashboard bootstrap. Mirrors main.js but swaps the
// single-trip SpeedProfileView for the DelayView (Segments / Stems
// modes). MapView and StreetViewPopup are unchanged.

import { State } from "./state.js";
import { MapView } from "./map_view.js";
import { DelayView } from "./delay_view.js";
import { StreetViewPopup } from "./street_view.js";
import { mountViewSwitcher } from "./view_switcher.js";

async function main() {
  const data = await fetch("./data.json").then(r => r.json());
  document.title = data.view_title || data.view_id;
  mountViewSwitcher(data);

  const state = new State();
  const profileView = new DelayView(
    document.getElementById("profile"), data, state
  );
  const mapView = new MapView(
    document.getElementById("map"), data, state
  );
  const streetView = new StreetViewPopup(data, state);

  // Global keyboard shortcuts. Bypassed when typing into form fields or
  // when a modifier is held. H toggles hide-without-delay; S / M switch
  // x-axis mode between Segments and steMs.
  document.addEventListener("keydown", (event) => {
    if (event.ctrlKey || event.metaKey || event.altKey) return;
    const tag = (event.target.tagName || "").toLowerCase();
    if (tag === "input" || tag === "textarea" || tag === "select") return;
    switch (event.key.toLowerCase()) {
      case "h":
        state.publish("hideUnattributed:changed",
          { value: !profileView.hideUnattributed });
        break;
      case "s":
        state.publish("delayMode:changed", { value: "segments" });
        break;
      case "m":
        state.publish("delayMode:changed", { value: "stems" });
        break;
      default:
        return;
    }
    event.preventDefault();
  });

  window._dashboard = { data, state, profileView, mapView, streetView };
}

main().catch(err => {
  console.error(err);
  document.body.innerHTML =
    `<pre style="padding:20px;color:#c00;font-family:monospace">${err.stack || err}</pre>`;
});
