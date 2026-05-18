// Dashboard bootstrap. Loads data.json, instantiates the State store +
// both views, and wires them together. Add new views (e.g. a route-distance
// timeline) by importing another class and constructing it here — every
// view shares the same State pub/sub so coordination is automatic.

import { State } from "./state.js";
import { MapView } from "./map_view.js";
import { SpeedProfileView } from "./speed_profile_view.js";

async function main() {
  const data = await fetch("./data.json").then(r => r.json());
  document.title = data.view_title || data.view_id;

  const state = new State();
  const profileView = new SpeedProfileView(
    document.getElementById("profile"), data, state
  );
  const mapView = new MapView(
    document.getElementById("map"), data, state
  );

  // Hand both views a reference to the data + state, then let them drive
  // themselves. The map will publish range:changed once it loads, which
  // gives the profile its initial x-domain.
  window._dashboard = { data, state, profileView, mapView };  // for debugging
}

main().catch(err => {
  console.error(err);
  document.body.innerHTML =
    `<pre style="padding:20px;color:#c00;font-family:monospace">${err.stack || err}</pre>`;
});
