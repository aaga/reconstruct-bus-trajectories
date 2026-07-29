// Network payload loading + client-side stats combination.
//
// Mirrors analysis/network/stats.py exactly (Welford merge, histogram
// quantiles) — data/network/golden.json is the parity fixture; see
// selfTestGolden(). Shards are packed columnar binaries (see
// build_payloads.py for the layout).

const N_BUCKETS = 16;

// Strip raw OSM node ids from human-facing labels ("node 4332637067" adds
// clutter and means nothing to a rider). "A → node N" reads as mid-block.
export function cleanLabel(label) {
  if (!label) return label;
  return label.replace(/node \d+/g, "mid-block");
}

export class NetworkData {
  constructor(baseUrl = "../data/network") {
    this.base = baseUrl;
    this.meta = null;
    this.segments = null; // GeoJSON FeatureCollection
    this.shards = new Map(); // period -> decoded columns
  }

  async init() {
    const [meta, segments] = await Promise.all([
      fetch(`${this.base}/meta.json`).then((r) => r.json()),
      fetch(`${this.base}/segments.json`).then((r) => r.json()),
    ]);
    this.meta = meta;
    this.segments = segments;
    return this;
  }

  async loadShard(period) {
    // Cache the in-flight promise so concurrent combines share one download.
    if (this.shards.has(period)) return this.shards.get(period);
    const promise = this._fetchShard(period);
    this.shards.set(period, promise);
    try {
      const cols = await promise;
      this.shards.set(period, Promise.resolve(cols));
      return cols;
    } catch (e) {
      this.shards.delete(period); // allow retry after a transient failure
      throw e;
    }
  }

  async _fetchShard(period) {
    let buf = null;
    // Prefer the pre-gzipped twin (~3-4x smaller; Pages won't compress .bin).
    if (typeof DecompressionStream === "function") {
      const r = await fetch(`${this.base}/stats_${period}.bin.gz`);
      if (r.ok) {
        const ds = r.body.pipeThrough(new DecompressionStream("gzip"));
        buf = await new Response(ds).arrayBuffer();
      }
    }
    if (!buf) {
      buf = await fetch(`${this.base}/stats_${period}.bin`).then((r) => {
        if (!r.ok) throw new Error(`shard ${period}: HTTP ${r.status}`);
        return r.arrayBuffer();
      });
    }
    return decodeShard(buf);
  }

  // ---- filter combination ------------------------------------------------

  // filters: {periods: [..], routes: [rid ints]|null, pick, season, weather,
  //           dow (0-6)|null, daytype: "weekday"|"sat"|"sun"|null}
  // Returns Map<sid, {n, sum, m2, hist: Float64Array}>
  async combine(filters) {
    const out = new Map();
    for (const period of filters.periods) {
      const c = await this.loadShard(period);
      const routeSet = filters.routes && filters.routes.length ? new Set(filters.routes) : null;
      for (let i = 0; i < c.n_rows; i++) {
        if (routeSet && !routeSet.has(c.rid[i])) continue;
        if (filters.pick != null && c.pick[i] !== filters.pick) continue;
        if (filters.season != null && c.season[i] !== filters.season) continue;
        if (filters.weather != null && c.weather[i] !== filters.weather) continue;
        if (filters.dow != null) {
          if (c.dow[i] !== filters.dow) continue;
        } else if (filters.daytype === "weekday") {
          if (c.dow[i] >= 5) continue;
        } else if (filters.daytype === "sat") {
          if (c.dow[i] !== 5) continue;
        } else if (filters.daytype === "sun") {
          if (c.dow[i] !== 6) continue;
        }
        const sid = c.sid[i];
        let acc = out.get(sid);
        if (!acc) {
          acc = { n: 0, sum: 0, m2: 0, hist: new Float64Array(N_BUCKETS),
                  nDoor: 0, sumDwell: 0, sumDelayDoor: 0, sumOns: 0, sumOffs: 0, sumLoad: 0 };
          out.set(sid, acc);
        }
        const merged = welfordMerge(acc.n, acc.sum, acc.m2, c.n[i], c.sum_delay[i], c.m2[i]);
        acc.n = merged[0];
        acc.sum = merged[1];
        acc.m2 = merged[2];
        for (let b = 0; b < N_BUCKETS; b++) acc.hist[b] += c.hist[b][i];
        if (c.n_door) {
          acc.nDoor += c.n_door[i];
          acc.sumDwell += c.sum_dwell[i];
          acc.sumDelayDoor += c.sum_delay_door[i];
          acc.sumOns += c.sum_ons[i];
          acc.sumOffs += c.sum_offs[i];
          acc.sumLoad += c.sum_load[i];
        }
      }
    }
    return out;
  }

  // Service-date count matching the (pick, season, weather, dow/daytype)
  // parts of a filter — the buses/hour denominator.
  dateCount(filters) {
    let total = 0;
    for (const [key, count] of Object.entries(this.meta.date_counts)) {
      const [pick, season, dow, weather] = key.split("|").map(Number);
      if (filters.pick != null && pick !== filters.pick) continue;
      if (filters.season != null && season !== filters.season) continue;
      if (filters.weather != null && weather !== filters.weather) continue;
      if (filters.dow != null) {
        if (dow !== filters.dow) continue;
      } else if (filters.daytype === "weekday") {
        if (dow >= 5) continue;
      } else if (filters.daytype === "sat") {
        if (dow !== 5) continue;
      } else if (filters.daytype === "sun") {
        if (dow !== 6) continue;
      }
      total += count;
    }
    return total;
  }

  // Same as dateCount but over door-covered service dates only — the
  // denominator for boardings/hour and any door-derived rate.
  doorDateCount(filters) {
    let total = 0;
    for (const [key, count] of Object.entries(this.meta.door_date_counts ?? {})) {
      const [pick, season, dow, weather] = key.split("|").map(Number);
      if (filters.pick != null && pick !== filters.pick) continue;
      if (filters.season != null && season !== filters.season) continue;
      if (filters.weather != null && weather !== filters.weather) continue;
      if (filters.dow != null) {
        if (dow !== filters.dow) continue;
      } else if (filters.daytype === "weekday") {
        if (dow >= 5) continue;
      } else if (filters.daytype === "sat") {
        if (dow !== 5) continue;
      } else if (filters.daytype === "sun") {
        if (dow !== 6) continue;
      }
      total += count;
    }
    return total;
  }

  periodHours(filters) {
    let h = 0;
    for (const p of filters.periods) h += this.meta.period_hours[p] || 0;
    return h;
  }
}

// --------------------------------------------------------------------------
// Shard decoding
// --------------------------------------------------------------------------

const DTYPE = {
  "<u1": [Uint8Array, 1],
  "<u2": [Uint16Array, 2],
  "<f4": [Float32Array, 4],
};

export function decodeShard(buf) {
  const dv = new DataView(buf);
  const magic = new TextDecoder().decode(new Uint8Array(buf, 0, 8));
  if (magic !== "NWSTATS1") throw new Error(`bad shard magic: ${magic}`);
  const hlen = dv.getUint32(8, true);
  const header = JSON.parse(new TextDecoder().decode(new Uint8Array(buf, 12, hlen)));
  let off = 12 + hlen;
  const out = { n_rows: header.n_rows, hist: [] };
  for (const col of header.columns) {
    const [Ctor, size] = DTYPE[col.dtype];
    // Views require aligned offsets; copy instead (shards are a few MB).
    const bytes = buf.slice(off, off + header.n_rows * size);
    const arr = new Ctor(bytes);
    off += header.n_rows * size;
    if (/^h\d+$/.test(col.name)) out.hist[Number(col.name.slice(1))] = arr;
    else out[col.name] = arr;
  }
  return out;
}

// --------------------------------------------------------------------------
// Stats math (mirror of stats.py)
// --------------------------------------------------------------------------

export function welfordMerge(n1, s1, m2_1, n2, s2, m2_2) {
  const n = n1 + n2;
  if (n === 0) return [0, 0, 0];
  if (n1 === 0) return [n2, s2, m2_2];
  if (n2 === 0) return [n1, s1, m2_1];
  const delta = s2 / n2 - s1 / n1;
  return [n, s1 + s2, m2_1 + m2_2 + (delta * delta * n1 * n2) / n];
}

export function quantileFromHist(hist, edges, underEdge, overEdge, q) {
  let total = 0;
  for (let i = 0; i < hist.length; i++) total += hist[i];
  if (total === 0) return NaN;
  const target = q * total;
  const lo = [underEdge, ...edges];
  const hi = [...edges, overEdge];
  let cum = 0;
  for (let i = 0; i < hist.length; i++) {
    const c = hist[i];
    if (c > 0 && cum + c >= target) {
      const frac = (target - cum) / c;
      return lo[i] + frac * (hi[i] - lo[i]);
    }
    cum += c;
  }
  return hi[hi.length - 1];
}

// All display metrics for one segment's combined accumulator.
export function deriveMetrics(acc, tFf, meta) {
  const { n, sum, m2, hist } = acc;
  const mean = n ? sum / n : NaN;
  const std = n > 1 ? Math.sqrt(m2 / n) : n === 1 ? 0 : NaN;
  const r50 = quantileFromHist(hist, meta.hist_edges, meta.under_edge, meta.over_edge, 0.5);
  const r90 = quantileFromHist(hist, meta.hist_edges, meta.under_edge, meta.over_edge, 0.9);
  const nDoor = acc.nDoor ?? 0;
  const out = {
    n,
    mean_delay_s: mean,
    std_delay_s: std,
    median_delay_s: n ? (r50 - 1) * tFf : NaN,
    p90_delay_s: n ? (r90 - 1) * tFf : NaN,
    buffer_s: n ? (r90 - r50) * tFf : NaN,
    n_door: nDoor,
    mean_dwell_s: NaN,
    moving_delay_s: NaN,
    dwell_share: NaN,
  };
  if (nDoor > 0) {
    const meanDwell = acc.sumDwell / nDoor;
    const meanDelayDoor = acc.sumDelayDoor / nDoor;
    out.mean_dwell_s = meanDwell;
    out.moving_delay_s = meanDelayDoor - meanDwell;
    out.dwell_share = acc.sumDelayDoor > 1e-9 ? acc.sumDwell / acc.sumDelayDoor : NaN;
  }
  return out;
}

// --------------------------------------------------------------------------
// Golden-vector parity check (run from the console or a dev flag)
// --------------------------------------------------------------------------

export async function selfTestGolden(baseUrl = "../data/network") {
  const g = await fetch(`${baseUrl}/golden.json`).then((r) => r.json());
  const meta = { hist_edges: g.hist_edges, under_edge: g.under_edge, over_edge: g.over_edge };
  const failures = [];
  for (const c of g.cases) {
    const acc = { n: c.n, sum: c.sum_delay, m2: c.m2, hist: Float64Array.from(c.hist),
                  nDoor: c.n_door ?? 0, sumDwell: c.sum_dwell ?? 0,
                  sumDelayDoor: c.sum_delay_door ?? 0, sumOns: 0, sumOffs: 0, sumLoad: 0 };
    const got = deriveMetrics(acc, c.t_ff_s, meta);
    for (const [k, want] of Object.entries(c.metrics)) {
      const g_ = got[k];
      const ok =
        want == null
          ? Number.isNaN(g_)
          : Math.abs(g_ - want) <= 1e-6 * Math.max(1, Math.abs(want));
      if (!ok) failures.push(`case ${c.case} ${k}: got ${g_}, want ${want}`);
    }
    const [mn, ms, mm2] = c.merged_equals_whole[0];
    const merged = welfordMerge(0, 0, 0, mn, ms, mm2);
    if (merged[0] !== c.n) failures.push(`case ${c.case}: merge n mismatch`);
  }
  if (failures.length) {
    console.error("network_data golden parity FAILED:", failures);
    return false;
  }
  console.log(`network_data golden parity OK (${g.cases.length} cases)`);
  return true;
}
