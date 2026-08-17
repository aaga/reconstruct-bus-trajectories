// Explore: arbitrary slices of the date-grain fact table, queried in the
// browser with DuckDB-WASM over HTTP range requests.
//
// The packed .bin shards answer a fixed set of pre-planned slices and must
// be downloaded whole; across 2.5 years that is several GB before the first
// pixel. Here the browser runs real SQL against hive-partitioned parquet and
// pulls only the row groups a slice touches, so date range x day-of-week x
// period x route x turn-movement x near/far-side all compose freely.
//
// Every measure in the fact table is additive, so a coarser slice is a SUM:
// mean delay = sum_delay/n, and variance comes from sum_delay_sq.

const DUCKDB_BASE = "./vendor/duckdb";
const FACTS = "./data/network/facts";

const DOWS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const PERIODS = ["am_peak", "midday", "pm_peak", "evening", "late_night"];
const MVMTS = { T: "Through", L: "Left", R: "Right", E: "Exit/other" };
const METRICS = {
  mean_delay:  { label: "Mean delay (s/trip)", expr: "sum(sum_delay)/nullif(sum(n),0)" },
  mean_ratio:  { label: "Delay ratio (t_obs/t_ff)", expr: "sum(sum_t_obs)/nullif(sum(sum_t_ff),0)" },
  pax_delay:   { label: "Passenger-seconds / trip", expr: "sum(sum_pax_s)/nullif(sum(n),0)" },
  dwell_share: { label: "Dwell share of delay", expr: "sum(sum_dwell_s)/nullif(sum(sum_delay),0)" },
  nd_delay:    { label: "Non-dwell delay (s/trip)", expr: "sum(sum_nd_s)/nullif(sum(n),0)" },
  trips:       { label: "Traversals", expr: "sum(n)" },
};

export class ExploreView {
  constructor(state) {
    this.S = state;
    this.db = null;
    this.conn = null;
    this.ready = false;
    this.meta = null;
    this.q = {
      from: null, to: null,
      dows: new Set([0, 1, 2, 3, 4]),
      periods: new Set(["am_peak", "pm_peak"]),
      route: "", mvmt: "", side: "", metric: "mean_delay", minN: 30,
    };
  }

  // ---- duckdb bootstrap (lazy: only when the tab is first opened) --------
  async _boot(host) {
    if (this.ready) return true;
    const note = (m) => { host.querySelector(".xp-status").textContent = m; };
    try {
      note("loading query engine (~34 MB, cached after first load)…");
      const duckdb = await import(`${DUCKDB_BASE}/duckdb-browser.mjs`);
      const worker = new Worker(`${DUCKDB_BASE}/duckdb-browser-eh.worker.js`,
                                { type: "module" });
      this.db = new duckdb.AsyncDuckDB(new duckdb.ConsoleLogger("WARNING"), worker);
      await this.db.instantiate(`${DUCKDB_BASE}/duckdb-eh.wasm`);
      this.conn = await this.db.connect();
      this._duckdb = duckdb;

      note("indexing fact table…");
      this.meta = await (await fetch(`${FACTS}/meta.json`, { cache: "no-cache" })).json();
      // Register each month's parquet by URL; duckdb fetches byte ranges.
      const base = new URL(FACTS, location.href).href;
      for (const ym of this.meta.months) {
        const [y, m] = ym.split("-");
        const name = `f_${y}_${m}.parquet`;
        await this.db.registerFileURL(
          name, `${base}/year=${y}/month=${m}/part-0.parquet`,
          this._duckdb.DuckDBDataProtocol.HTTP, false);
      }
      for (const d of ["dim_segments", "dim_dates", "dim_routes"]) {
        await this.db.registerFileURL(`${d}.parquet`, `${base}/${d}.parquet`,
                                      this._duckdb.DuckDBDataProtocol.HTTP, false);
      }
      const files = this.meta.months
        .map((ym) => `'f_${ym.split("-")[0]}_${ym.split("-")[1]}.parquet'`)
        .join(", ");
      await this.conn.query(
        `CREATE OR REPLACE VIEW facts AS SELECT * FROM read_parquet([${files}])`);
      await this.conn.query(
        "CREATE OR REPLACE VIEW dseg AS SELECT * FROM 'dim_segments.parquet'");
      await this.conn.query(
        "CREATE OR REPLACE VIEW ddate AS SELECT * FROM 'dim_dates.parquet'");
      this.ready = true;
      note("");
      return true;
    } catch (e) {
      note(`query engine failed to load: ${e.message}`);
      return false;
    }
  }

  _where() {
    const q = this.q;
    const w = [`f.service_date BETWEEN DATE '${q.from}' AND DATE '${q.to}'`];
    if (q.dows.size && q.dows.size < 7)
      w.push(`d.dow IN (${[...q.dows].join(",")})`);
    if (q.periods.size && q.periods.size < PERIODS.length)
      w.push(`f.period IN (${[...q.periods].map((p) => `'${p}'`).join(",")})`);
    if (q.route) w.push(`f.route_id = '${q.route.replace(/'/g, "''")}'`);
    if (q.mvmt) w.push(`f.mvm = '${q.mvmt}'`);
    if (q.side === "near") w.push("s.n_near_side > 0");
    if (q.side === "far") w.push("s.n_far_side > 0");
    if (q.side === "none") w.push("s.n_near_side = 0 AND s.n_far_side = 0");
    return w.join(" AND ");
  }

  async _run(host) {
    if (!await this._boot(host)) return;
    const q = this.q;
    const m = METRICS[q.metric];
    const status = host.querySelector(".xp-status");
    status.textContent = "querying…";
    const t0 = performance.now();
    try {
      const sql = `
        SELECT s.seg_id, any_value(s.label) AS label,
               sum(f.n)::BIGINT AS n,
               ${m.expr} AS metric,
               sum(f.sum_delay)/nullif(sum(f.n),0) AS mean_delay,
               sum(f.sum_pax_s)/nullif(sum(f.n),0) AS pax_per_trip
        FROM facts f
        JOIN ddate d ON d.service_date = f.service_date
        JOIN dseg  s ON s.seg_id = f.seg_id
        WHERE ${this._where()}
        GROUP BY s.seg_id
        HAVING sum(f.n) >= ${q.minN}
        ORDER BY metric DESC NULLS LAST
        LIMIT 60`;
      const res = await this.conn.query(sql);
      const rows = res.toArray().map((r) => r.toJSON());
      // Trend: the same slice aggregated by month.
      const trend = (await this.conn.query(`
        SELECT strftime(f.service_date, '%Y-%m') AS ym,
               ${m.expr} AS metric, sum(f.n)::BIGINT AS n
        FROM facts f
        JOIN ddate d ON d.service_date = f.service_date
        JOIN dseg  s ON s.seg_id = f.seg_id
        WHERE ${this._where()}
        GROUP BY 1 ORDER BY 1`)).toArray().map((r) => r.toJSON());
      const ms = Math.round(performance.now() - t0);
      status.textContent =
        `${rows.length} segments · ${trend.reduce((a, r) => a + Number(r.n), 0).toLocaleString()} traversals · ${ms} ms`;
      this._renderResults(host, rows, trend, m);
    } catch (e) {
      status.textContent = `query failed: ${e.message}`;
    }
  }

  _renderResults(host, rows, trend, metric) {
    const fmt = (v) => (v == null ? "—"
      : Math.abs(v) >= 100 ? Number(v).toFixed(0)
      : Number(v).toFixed(2));
    const body = host.querySelector(".xp-results");
    // sparkline over months
    let spark = "";
    if (trend.length > 1) {
      const vals = trend.map((r) => Number(r.metric) || 0);
      const lo = Math.min(...vals), hi = Math.max(...vals);
      const W = 620, H = 90, pad = 26;
      const x = (i) => pad + (i / (trend.length - 1)) * (W - pad - 8);
      const y = (v) => H - 18 - ((v - lo) / (hi - lo || 1)) * (H - 34);
      const pts = vals.map((v, i) => `${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
      spark = `<svg class="xp-spark" viewBox="0 0 ${W} ${H}" width="100%" height="${H}">
        <polyline points="${pts}" fill="none" stroke="#2f6fd6" stroke-width="1.8"/>
        ${vals.map((v, i) => `<circle cx="${x(i).toFixed(1)}" cy="${y(v).toFixed(1)}" r="2" fill="#2f6fd6"/>`).join("")}
        <text x="2" y="12" font-size="10" fill="#666">${fmt(hi)}</text>
        <text x="2" y="${H - 20}" font-size="10" fill="#666">${fmt(lo)}</text>
        <text x="${pad}" y="${H - 4}" font-size="10" fill="#666">${trend[0].ym}</text>
        <text x="${W - 60}" y="${H - 4}" font-size="10" fill="#666">${trend.at(-1).ym}</text>
      </svg>`;
    }
    body.innerHTML = `
      ${spark ? `<div class="xp-trend"><h4>${metric.label} by month</h4>${spark}</div>` : ""}
      <table class="xp-table">
        <thead><tr><th>#</th><th>Segment</th><th>${metric.label}</th>
          <th>Mean delay</th><th>Pax·s/trip</th><th>Traversals</th></tr></thead>
        <tbody>${rows.map((r, i) => `
          <tr data-seg="${r.seg_id}">
            <td>${i + 1}</td><td>${r.label ?? r.seg_id}</td>
            <td><b>${fmt(r.metric)}</b></td><td>${fmt(r.mean_delay)}</td>
            <td>${fmt(r.pax_per_trip)}</td><td>${Number(r.n).toLocaleString()}</td>
          </tr>`).join("")}</tbody>
      </table>`;
  }

  async render(host) {
    const q = this.q;
    if (!q.from) {
      try {
        const meta = await (await fetch(`${FACTS}/meta.json`, { cache: "no-cache" })).json();
        q.from = `${meta.months[0]}-01`;
        const last = meta.months.at(-1);
        q.to = `${last}-28`;
      } catch { q.from = "2024-01-01"; q.to = "2026-08-01"; }
    }
    host.innerHTML = `
      <div class="xp-wrap">
        <div class="xp-controls">
          <label>From <input type="date" class="xp-from" value="${q.from}"></label>
          <label>To <input type="date" class="xp-to" value="${q.to}"></label>
          <span class="xp-group">Days:${DOWS.map((d, i) =>
            `<label><input type="checkbox" data-dow="${i}" ${q.dows.has(i) ? "checked" : ""}>${d}</label>`).join("")}</span>
          <span class="xp-group">Period:${PERIODS.map((p) =>
            `<label><input type="checkbox" data-period="${p}" ${q.periods.has(p) ? "checked" : ""}>${p.replace("_", " ")}</label>`).join("")}</span>
          <label>Route <input class="xp-route" size="5" value="${q.route}" placeholder="all"></label>
          <label>Movement <select class="xp-mvmt">
            <option value="">all</option>
            ${Object.entries(MVMTS).map(([k, v]) =>
              `<option value="${k}" ${q.mvmt === k ? "selected" : ""}>${v}</option>`).join("")}
          </select></label>
          <label>Stops <select class="xp-side">
            <option value="">any</option>
            <option value="near" ${q.side === "near" ? "selected" : ""}>has near-side</option>
            <option value="far" ${q.side === "far" ? "selected" : ""}>has far-side</option>
            <option value="none" ${q.side === "none" ? "selected" : ""}>no stops at signal</option>
          </select></label>
          <label>Rank by <select class="xp-metric">
            ${Object.entries(METRICS).map(([k, v]) =>
              `<option value="${k}" ${q.metric === k ? "selected" : ""}>${v.label}</option>`).join("")}
          </select></label>
          <label>min n <input class="xp-minn" size="4" value="${q.minN}"></label>
          <button class="xp-run">Run</button>
        </div>
        <div class="xp-status"></div>
        <div class="xp-results"></div>
      </div>`;

    const $ = (s) => host.querySelector(s);
    $(".xp-run").onclick = () => {
      q.from = $(".xp-from").value; q.to = $(".xp-to").value;
      q.dows = new Set([...host.querySelectorAll("[data-dow]")]
        .filter((c) => c.checked).map((c) => +c.dataset.dow));
      q.periods = new Set([...host.querySelectorAll("[data-period]")]
        .filter((c) => c.checked).map((c) => c.dataset.period));
      q.route = $(".xp-route").value.trim();
      q.mvmt = $(".xp-mvmt").value;
      q.side = $(".xp-side").value;
      q.metric = $(".xp-metric").value;
      q.minN = Math.max(1, +$(".xp-minn").value || 1);
      this._run(host);
    };
  }
}
