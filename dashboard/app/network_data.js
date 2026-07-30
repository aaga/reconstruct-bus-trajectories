// Network payload loading + client-side stats combination.
//
// Mirrors analysis/network/stats.py exactly (Welford merge, histogram
// quantiles) — data/network/golden.json is the parity fixture; see
// selfTestGolden(). Shards are packed columnar binaries (see
// build_payloads.py for the layout).

const N_BUCKETS = 16;

// Cities served by the Network tab. Payloads for the first city live at the
// legacy flat path; later cities nest under their id (build_payloads --out /
// build_distributions mirror this layout).
export const NETWORK_CITIES = {
  cta: { label: "Chicago", base: "../data/network" },
  mbta: { label: "Boston", base: "../data/network/mbta" },
};

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
    const parts = this.meta.shards?.[period]?.parts?.map((p) => p.name)
      ?? [`stats_${period}.bin`];
    const decoded = await Promise.all(parts.map((name) => this._fetchPart(name)));
    return decoded; // array of column blocks; combine() iterates them
  }

  async _fetchPart(name) {
    let buf = null;
    // Prefer the pre-gzipped twin (~3x smaller; Pages won't compress .bin).
    if (typeof DecompressionStream === "function") {
      const r = await fetch(`${this.base}/${name}.gz`);
      if (r.ok) {
        const ds = r.body.pipeThrough(new DecompressionStream("gzip"));
        buf = await new Response(ds).arrayBuffer();
      }
    }
    if (!buf) {
      buf = await fetch(`${this.base}/${name}`).then((r) => {
        if (!r.ok) throw new Error(`shard part ${name}: HTTP ${r.status}`);
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
      const blocks = await this.loadShard(period);
      const routeSet = filters.routes && filters.routes.length ? new Set(filters.routes) : null;
      for (const c of blocks) {
      for (let i = 0; i < c.n_rows; i++) {
        if (routeSet && !routeSet.has(c.rid[i])) continue;
        if (filters.pick != null && c.pick[i] !== filters.pick) continue;
        if (filters.season != null && c.season[i] !== filters.season) continue;
        if (filters.weather != null && c.weather[i] !== filters.weather) continue;
        if (filters.dow != null) {
          if (c.dow[i] !== filters.dow) continue;
        } else if (filters.daytype === "weekday") {
          if (c.dow[i] >= 5) continue;
        } else if (filters.daytype === "weekend") {
          if (c.dow[i] !== 5 && c.dow[i] !== 6) continue;
        } else if (filters.daytype === "sat") {
          if (c.dow[i] !== 5) continue;
        } else if (filters.daytype === "sun") {
          if (c.dow[i] !== 6) continue;
        }
        // daytype null/"everyday": everything, holidays (dow=7) included
        const sid = c.sid[i];
        let acc = out.get(sid);
        if (!acc) {
          acc = { n: 0, sum: 0, m2: 0, hist: new Float64Array(N_BUCKETS),
                  nDoor: 0, sumDwell: 0, sumDelayDoor: 0, sumNd: 0, m2Dw: 0, m2Nd: 0,
                  sumPax: 0, m2Pax: 0, sumOns: 0, sumOffs: 0, sumLoad: 0,
                  hist_dw: new Float64Array(N_BUCKETS),
                  hist_nd: new Float64Array(N_BUCKETS),
                  hist_pax: new Float64Array(N_BUCKETS) };
          out.set(sid, acc);
        }
        const merged = welfordMerge(acc.n, acc.sum, acc.m2, c.n[i], c.sum_delay[i], c.m2[i]);
        acc.n = merged[0];
        acc.sum = merged[1];
        acc.m2 = merged[2];
        for (let b = 0; b < N_BUCKETS; b++) acc.hist[b] += c.hist[b][i];
        if (c.n_door) {
          const nd = c.n_door[i];
          // Welford-merge each door-subset family (n = door-covered count).
          const dw = welfordMerge(acc.nDoor, acc.sumDwell, acc.m2Dw, nd, c.sum_dwell[i], c.m2_dw?.[i] ?? 0);
          const ndl = welfordMerge(acc.nDoor, acc.sumNd, acc.m2Nd,
            nd, c.sum_nd?.[i] ?? 0, c.m2_nd?.[i] ?? 0);
          const px = welfordMerge(acc.nDoor, acc.sumPax, acc.m2Pax, nd, c.sum_pax?.[i] ?? 0, c.m2_pax?.[i] ?? 0);
          acc.m2Dw = dw[2];
          acc.m2Nd = ndl[2];
          acc.m2Pax = px[2];
          acc.nDoor += nd;
          acc.sumDwell += c.sum_dwell[i];
          acc.sumDelayDoor += c.sum_delay_door[i];
          acc.sumNd += c.sum_nd?.[i] ?? 0;
          acc.sumPax += c.sum_pax?.[i] ?? 0;
          acc.sumOns += c.sum_ons[i];
          acc.sumOffs += c.sum_offs[i];
          acc.sumLoad += c.sum_load[i];
          for (let b = 0; b < N_BUCKETS; b++) {
            acc.hist_dw[b] += c.hist_dw[b]?.[i] ?? 0;
            acc.hist_nd[b] += c.hist_nd[b]?.[i] ?? 0;
            acc.hist_pax[b] += c.hist_pax[b]?.[i] ?? 0;
          }
        }
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
      } else if (filters.daytype === "weekend") {
        if (dow !== 5 && dow !== 6) continue;
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
      } else if (filters.daytype === "weekend") {
        if (dow !== 5 && dow !== 6) continue;
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
  const out = { n_rows: header.n_rows, hist: [], hist_dw: [], hist_nd: [], hist_pax: [] };
  const famOf = { h: "hist", hd: "hist_dw", hn: "hist_nd", hp: "hist_pax" };
  for (const col of header.columns) {
    const [Ctor, size] = DTYPE[col.dtype];
    // Views require aligned offsets; copy instead (parts are a few MB).
    const bytes = buf.slice(off, off + header.n_rows * size);
    const arr = new Ctor(bytes);
    off += header.n_rows * size;
    const m = /^(hp|hn|hd|h)(\d+)$/.exec(col.name);
    if (m) out[famOf[m[1]]][Number(m[2])] = arr;
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

// Per-(family, stat) derivation — mirror of stats.derive_stat.
// families: overall | pax | nondwell | dwell; stats: mean|median|std|p95|buffer
export function deriveStat(family, stat, acc, tFf, meta) {
  const fams = meta.hist_families;
  const block = (n, total, m2, hist, fam, toSeconds) => {
    if (!n) return NaN;
    const mean = total / n;
    if (stat === "mean") return mean;
    if (stat === "std") return n > 1 ? Math.sqrt(m2 / n) : 0;
    const q = stat === "median" ? 0.5 : 0.95;
    const f = fams[fam];
    let v = quantileFromHist(hist, f.edges, f.under, f.over, q);
    v = toSeconds(v);
    return stat === "buffer" ? v - mean : v;
  };
  if (family === "overall") {
    return block(acc.n, acc.sum, acc.m2, acc.hist, "ratio", (r) => (r - 1) * tFf);
  }
  const nd = acc.nDoor ?? 0;
  if (family === "dwell") {
    return block(nd, acc.sumDwell, acc.m2Dw, acc.hist_dw, "dw", (r) => r * tFf);
  }
  if (family === "nondwell") {
    // Event-classified non-dwell seconds (nd_event_s): 0-centric ratio to
    // t_ff on the dwell-style edges, so seconds = r * t_ff (no −1 shift).
    return block(nd, acc.sumNd, acc.m2Nd, acc.hist_nd, "nd", (r) => r * tFf);
  }
  if (family === "pax") {
    return block(nd, acc.sumPax, acc.m2Pax, acc.hist_pax, "pax", (v) => v);
  }
  throw new Error(`unknown family ${family}`);
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
  const meta = { hist_families: g.families };
  const failures = [];
  for (const c of g.cases) {
    const a = c.acc;
    const acc = {
      n: a.n, sum: a.sum, m2: a.m2, hist: Float64Array.from(a.hist),
      nDoor: a.n_door, sumDwell: a.sum_dwell, sumDelayDoor: a.sum_delay_door,
      sumNd: a.sum_nd, m2Dw: a.m2_dw, m2Nd: a.m2_nd, sumPax: a.sum_pax, m2Pax: a.m2_pax,
      hist_dw: Float64Array.from(a.hist_dw),
      hist_nd: Float64Array.from(a.hist_nd),
      hist_pax: Float64Array.from(a.hist_pax),
    };
    for (const [key, want] of Object.entries(c.expected)) {
      const [fam, st] = key.split(".");
      const got = deriveStat(fam, st, acc, c.t_ff_s, meta);
      const ok = want == null
        ? Number.isNaN(got)
        : Math.abs(got - want) <= 1e-4 * Math.max(1, Math.abs(want));
      if (!ok) failures.push(`case ${c.case} ${key}: got ${got}, want ${want}`);
    }
  }
  if (failures.length) {
    console.error("network_data golden parity FAILED:", failures.slice(0, 8));
    return false;
  }
  console.log(`network_data golden parity OK (${g.cases.length} cases)`);
  return true;
}
