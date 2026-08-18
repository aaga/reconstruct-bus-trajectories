"""Date-grain fact table for responsive querying (DuckDB-WASM).

The packed .bin shards answer a fixed set of pre-planned slices and must be
downloaded whole; at 2.5 years that is several GB before the first pixel.
This emits a columnar fact table the browser can query directly over HTTP
range requests, so an arbitrary date range / day-of-week / period / route /
turn-movement slice reads only the row groups it needs.

Grain: (seg_id, route_id, service_date, period, mvmt, dir)
Measures are all ADDITIVE, so any coarser slice is a SUM over rows:
    n, sum_t_obs, sum_t_ff, sum_delay, sum_delay_sq  (mean+variance)
    n_door, sum_nd_s, sum_dwell_s, sum_pax_s
    h0..h15  — 16-bucket histogram of t_obs/t_ff (stats.HIST_EDGES)

Layout: facts/year=YYYY/month=MM/part-0.parquet, sorted by seg_id so a
segment filter touches few row groups. Dimensions ride alongside:
    dim_segments.parquet  seg_id, sid, label, len_m, road_class, t_ff_s,
                          n_stops, n_near_side, n_far_side
    dim_dates.parquet     service_date, dow, daytype, season, weather, pick
    dim_routes.parquet    route_id

Usage:
    PYTHONPATH=src uv run python analysis/network/build_facts.py --city cta
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

import duckdb

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from analysis.network.stats import HIST_EDGES, N_BUCKETS  # noqa: E402
from analysis.network.traversals_view import create_canonical_view  # noqa: E402
from dataio.cities import get_city  # noqa: E402

MAX_GAP_S = 180.0
FLAG_TOUCHED_TERMINAL = 1


def _hist_cols(expr: str) -> str:
    out = []
    for b in range(N_BUCKETS):
        if b == 0:
            cond = f"{expr} < {HIST_EDGES[0]}"
        elif b == N_BUCKETS - 1:
            cond = f"{expr} >= {HIST_EDGES[-1]}"
        else:
            cond = f"{expr} >= {HIST_EDGES[b-1]} AND {expr} < {HIST_EDGES[b]}"
        out.append(f"sum(CASE WHEN {cond} THEN 1 ELSE 0 END)::INT AS h{b}")
    return ",\n           ".join(out)


def build(city_id: str, out_root: Path | None = None,
          only_months: list[str] | None = None) -> None:
    city = get_city(city_id)
    base = REPO / "outputs" / "network" / city.city_id
    registry = json.loads((base / "segment_registry.json").read_text())
    freeflow = json.loads((base / "freeflow.json").read_text())
    date_attrs = json.loads((base / "date_attrs.json").read_text())
    movements = {}
    mv_path = base / "movements.json"
    if mv_path.exists():
        movements = json.loads(mv_path.read_text())

    out = out_root or (REPO / "dashboard" / "data" / "network" / "facts")
    if out.exists() and not only_months:
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect()
    con.execute(f"SET temp_directory='{base / 'duckdb_spill'}'")
    con.execute(f"SET memory_limit='{os.environ.get('FACTS_MEMORY_LIMIT', '10GB')}'")
    con.execute("SET preserve_insertion_order=false")
    con.execute("SET threads=3")

    trav_glob = str(base / "traversals" / "service_date=*" / "route=*.parquet")
    su = base / "event_sums"
    has_sums = bool(list(su.glob("service_date=*")))

    def scope_month(ym: str) -> None:
        """Point trav/es at ONE month's files.

        Building them over all 957 days and filtering by month in the WHERE
        clause makes every month's query join segmap across 10 GB of
        traversals — 7.4 GB of heap before it dies (2026-08-18). Scoping the
        globs lets duckdb read only that month.
        """
        create_canonical_view(
            con, trav_glob.replace("service_date=*", f"service_date={ym}-*"),
            registry, city)
        if has_sums:
            con.execute(f"""CREATE OR REPLACE VIEW es AS
                SELECT trip_key, seg_id, nd_event_s, dwell_union_s, pax_event_s
                FROM read_parquet(
                  '{su}/service_date={ym}-*/route=*.parquet')""")
        else:
            con.execute("""CREATE OR REPLACE VIEW es AS SELECT NULL::TEXT
                trip_key, NULL::TEXT seg_id, 0.0 nd_event_s,
                0.0 dwell_union_s, 0.0 pax_event_s WHERE FALSE""")

    con.execute("CREATE TABLE ff(seg_id TEXT, t_ff_s DOUBLE)")
    con.executemany("INSERT INTO ff VALUES (?, ?)",
                    [(k, v["t_ff_s"]) for k, v in freeflow["freeflow"].items()])
    # Era-complete: keyed by (shape_id, seg_id) for every era's shapes, not
    # just the canonical snapshot (see turn_movements.movement_rows).
    from analysis.network.turn_movements import movement_rows
    con.execute("CREATE TABLE mv(shape_id TEXT, seg_id TEXT, m TEXT)")
    mrows = movement_rows(city, registry)
    if mrows:
        con.executemany("INSERT INTO mv VALUES (?, ?, ?)", mrows)
    print(f"movements: {len(mrows):,} (shape, seg) pairs across all eras")

    months = sorted({d[:7] for d in date_attrs["days"]})
    if only_months:
        months = [m for m in months if m in only_months]
    t0 = time.time()
    n_rows = 0
    for ym in months:
        y, m = ym[:4], ym[5:7]
        part = out / f"year={y}" / f"month={m}"
        part.mkdir(parents=True, exist_ok=True)
        scope_month(ym)
        q = f"""
        COPY (
          WITH t AS (
            SELECT tr.seg_id, tr.route_id, tr.service_date, tr.period,
                   coalesce(mv.m, '?') AS mvm, tr.direction AS dir,
                   tr.t_obs_s, ff.t_ff_s,
                   tr.t_obs_s - ff.t_ff_s AS delay_s,
                   tr.t_obs_s / ff.t_ff_s AS ratio,
                   tr.has_door AS has_door,
                   coalesce(es.nd_event_s, 0.0)   AS nd_s,
                   coalesce(es.dwell_union_s, 0.0) AS dwell_s,
                   coalesce(es.pax_event_s, 0.0)  AS pax_s
            FROM trav tr
            JOIN ff ON ff.seg_id = tr.seg_id
            LEFT JOIN es ON es.trip_key = tr.trip_key AND es.seg_id = tr.seg_id
            LEFT JOIN mv ON mv.seg_id = tr.seg_id AND mv.shape_id = tr.shape_id
            WHERE tr.t_obs_s > 0 AND ff.t_ff_s > 0
              AND tr.max_gap_in_seg_s <= {MAX_GAP_S}
              AND (tr.flags & {FLAG_TOUCHED_TERMINAL}) = 0
          )
          SELECT seg_id, route_id, service_date, period, mvm, dir,
                 count(*)::INT              AS n,
                 sum(t_obs_s)               AS sum_t_obs,
                 sum(t_ff_s)                AS sum_t_ff,
                 sum(delay_s)               AS sum_delay,
                 sum(delay_s * delay_s)     AS sum_delay_sq,
                 sum(CASE WHEN has_door THEN 1 ELSE 0 END)::INT AS n_door,
                 sum(nd_s)                  AS sum_nd_s,
                 sum(dwell_s)               AS sum_dwell_s,
                 sum(pax_s)                 AS sum_pax_s,
                 {_hist_cols('ratio')}
          FROM t
          GROUP BY 1,2,3,4,5,6
          ORDER BY seg_id, route_id, service_date
        ) TO '{part / "part-0.parquet"}'
          (FORMAT PARQUET, COMPRESSION ZSTD, ROW_GROUP_SIZE 50000)
        """
        con.execute(q)
        c = con.execute(
            f"SELECT count(*) FROM read_parquet('{part / 'part-0.parquet'}')"
        ).fetchone()[0]
        n_rows += c
        print(f"  {ym}: {c:,} rows ({time.time() - t0:.0f}s)", flush=True)

    # ---- dimensions -------------------------------------------------------
    seg_rows = []
    for i, (seg_id, rec) in enumerate(sorted(registry["segments"].items())):
        stops = rec.get("stops_off", [])
        seg_rows.append((
            seg_id, i, rec.get("label", ""), float(rec["len_m"]),
            rec.get("road_class", ""),
            float(freeflow["freeflow"].get(seg_id, {}).get("t_ff_s") or 0.0),
            len(stops),
            sum(1 for s in stops if s.get("signal_side") == "near_side"),
            sum(1 for s in stops if s.get("signal_side") == "far_side"),
        ))
    con.execute("""CREATE TABLE dim_seg(seg_id TEXT, sid INT, label TEXT,
        len_m DOUBLE, road_class TEXT, t_ff_s DOUBLE, n_stops INT,
        n_near_side INT, n_far_side INT)""")
    con.executemany("INSERT INTO dim_seg VALUES (?,?,?,?,?,?,?,?,?)", seg_rows)
    con.execute(f"COPY dim_seg TO '{out / 'dim_segments.parquet'}' "
                "(FORMAT PARQUET, COMPRESSION ZSTD)")

    con.execute("""CREATE TABLE dim_date(service_date DATE, dow INT,
        daytype TEXT, season TEXT, weather TEXT, pick TEXT)""")
    con.executemany("INSERT INTO dim_date VALUES (?,?,?,?,?,?)", [
        (d, int(a["dow"]), a["daytype"], a["season"], a["weather"],
         a["pick"] or "")
        for d, a in date_attrs["days"].items()])
    con.execute(f"COPY dim_date TO '{out / 'dim_dates.parquet'}' "
                "(FORMAT PARQUET, COMPRESSION ZSTD)")

    con.execute(f"""COPY (SELECT DISTINCT route_id FROM
        read_parquet('{out}/year=*/month=*/part-0.parquet') ORDER BY 1)
        TO '{out / 'dim_routes.parquet'}' (FORMAT PARQUET, COMPRESSION ZSTD)""")

    size = sum(p.stat().st_size for p in out.rglob("*.parquet"))
    (out / "meta.json").write_text(json.dumps({
        "grain": ["seg_id", "route_id", "service_date", "period", "mvm", "dir"],
        "hist_edges": list(HIST_EDGES),
        "n_rows": n_rows,
        "months": months,
        "sha": registry["meta"]["intersections_sha256"][:12],
    }, indent=1))
    print(f"wrote {n_rows:,} fact rows across {len(months)} months, "
          f"{size / 2**20:.0f} MB -> {out}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--city", default="cta")
    ap.add_argument("--out", default=None)
    ap.add_argument("--months", default=None,
                    help="comma-separated YYYY-MM subset (testing)")
    a = ap.parse_args()
    build(a.city, Path(a.out) if a.out else None,
          a.months.split(",") if a.months else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
