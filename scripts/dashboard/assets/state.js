// Central pub/sub store. The Map and SpeedProfile views never talk to each
// other directly — they emit events into `State` and subscribe to others
// emitted by their peer. Keeping this thin makes it cheap to add a third
// view (e.g. a route-distance timeline) later.
//
// Events:
//   "dist:hovered"     — { distM, source: "map" | "profile" }
//   "dist:cleared"     — null
//   "feature:hovered"  — { featureId }
//   "feature:cleared"  — null
//   "range:changed"    — { visibleDistRangeM: [lo_m, hi_m] }
//   "basemap:changed"  — { value: "map" | "satellite" }
//
// `range:changed` is emitted exclusively by the map (which owns the zoom).
// The speed profile listens and updates its x-axis domain.

export class State {
  constructor() {
    this._subs = new Map();   // event-name -> Set<fn>
  }
  subscribe(event, fn) {
    if (!this._subs.has(event)) this._subs.set(event, new Set());
    this._subs.get(event).add(fn);
    return () => this._subs.get(event)?.delete(fn);
  }
  publish(event, payload) {
    const set = this._subs.get(event);
    if (!set) return;
    for (const fn of set) fn(payload);
  }
}
