// Central pub/sub store, ported from scripts/dashboard/assets/state.js so the
// map view and the speed chart can stay decoupled (cursor + street-view sync).

export class State {
  constructor() {
    this._subs = new Map();
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
