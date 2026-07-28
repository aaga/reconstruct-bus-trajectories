"""Areas-of-interest engine: rank unusual segments / corridors / routes.

Two families of contexts:
  * LEVEL — within one filter (e.g. weekday pm_peak), which entities have an
    unusually large normalized metric vs the network distribution?
  * DIFF — between two filters (midday→pm_peak, weekday→weekend, dry→rain,
    pickA→pickB), which entities' metric *changes* unusually vs how much
    everyone else's changes?

Scoring per context (see plan): robust z within entity kind
    z = (x − median) / (1.4826 · MAD)
sample-size shrinkage  z* = z · sqrt(n / (n + N0)),  N0 = 25
priority = z* · (1 + 0.5·log1p(buses_per_hour))     [weighted]
Both weighted and unweighted rankings are emitted; the UI toggles.

Metrics are normalized so long and short entities compare fairly:
    mean_delay_ratio   mean(t_obs)/t_ff        (per traversal, pooled)
    median_delay_ratio p50(t_obs/t_ff)
    buffer_ratio       p90(ratio) − p50(ratio)
    cv_delay           std(delay)/t_ff

Quantiles here are EXACT (duckdb over raw traversals) — the dashboard's
histogram approximation is never used for rankings.

Usage (after the full batch + freeflow):
    PYTHONPATH=src uv run python analysis/network/areas_of_interest.py --city cta
Output:
    outputs/network/<city>/areas.json   (copied into dashboard payloads by
    build_payloads --with-areas or manually)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import duckdb
import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from analysis.network.traversals_view import create_canonical_view  # noqa: E402
from dataio.cities import CityConfig, get_city  # noqa: E402

N0_SHRINKAGE = 25
TOP_K = 50
MIN_N = 30  # entity needs this many traversals in a context to be ranked
MAX_GAP_S = 180.0
FLAG_TOUCHED_TERMINAL = 1

METRICS = ["mean_delay_ratio", "median_delay_ratio", "buffer_ratio", "cv_delay"]

LEVEL_FILTERS = [
    ("weekday", p) for p in ("am_peak", "midday", "pm_peak", None)
] + [("weekend", p) for p in ("midday", None)]

DIFF_PAIRS = [
    ("midday_to_am_peak", ("weekday", "midday"), ("weekday", "am_peak")),
    ("midday_to_pm_peak", ("weekday", "midday"), ("weekday", "pm_peak")),
    ("weekday_to_weekend", ("weekday", None), ("weekend", None)),
    ("dry_to_rain", ("weekday:dry", None), ("weekday:rain", None)),
    ("dry_to_snow", ("weekday:dry", None), ("weekday:snow", None)),
]


def robust_z(values: np.ndarray) -> np.ndarray:
    med = np.median(values)
    mad = np.median(np.abs(values - med))
    if mad == 0:
        return np.zeros_like(values)
    return (values - med) / (1.4826 * mad)


def shrunk(z: np.ndarray, n: np.ndarray, n0: float = N0_SHRINKAGE) -> np.ndarray:
    return z * np.sqrt(n / (n + n0))


def _filter_sql(daytype_weather: str, period: str | None) -> str:
    """WHERE fragment for a context filter spec like 'weekday' / 'weekday:rain'."""
    parts = daytype_weather.split(":")
    daytype = parts[0]
    conds = []
    if daytype == "weekday":
        conds.append("da.daytype = 'weekday'")
    elif daytype == "weekend":
        conds.append("da.daytype IN ('sat', 'sun')")
    else:
        conds.append(f"da.pick = '{daytype}'")
    if len(parts) > 1:
        conds.append(f"da.weather = '{parts[1]}'")
    if period:
        conds.append(f"t.period = '{period}'")
    return " AND ".join(conds)


def _entity_sql(kind: str) -> tuple[str, str]:
    """(entity id expression, extra join) per entity kind."""
    if kind == "segment":
        return "t.seg_id", ""
    if kind == "route":
        return "t.route_id", ""
    if kind == "corridor":
        return "cm.cid", "JOIN cormap cm ON cm.seg_id = t.seg_id"
    raise ValueError(kind)


def _metrics_query(kind: str, where: str) -> str:
    ent, join = _entity_sql(kind)
    return f"""
    SELECT
      {ent} AS eid,
      COUNT(*)::BIGINT AS n,
      AVG(t.ratio) - 1.0 AS mean_delay_ratio,
      QUANTILE_CONT(t.ratio, 0.5) - 1.0 AS median_delay_ratio,
      QUANTILE_CONT(t.ratio, 0.9) - QUANTILE_CONT(t.ratio, 0.5) AS buffer_ratio,
      STDDEV_POP(t.delay_s) / AVG(t.t_ff_s) AS cv_delay,
      COUNT(DISTINCT t.date_iso) AS n_dates,
      COUNT(DISTINCT t.trip_key) AS n_trips
    FROM t
    JOIN da ON da.date_iso = t.date_iso
    {join}
    WHERE {where}
    GROUP BY 1
    HAVING COUNT(*) >= {MIN_N}
    """


def build_areas(city: CityConfig) -> dict:
    base = REPO / "outputs" / "network" / city.city_id
    registry = json.loads((base / "segment_registry.json").read_text())
    corridors = json.loads((base / "corridors.json").read_text())
    freeflow = json.loads((base / "freeflow.json").read_text())
    date_attrs = json.loads((base / "date_attrs.json").read_text())
    glob = str(base / "traversals" / "service_date=*" / "route=*.parquet")

    con = duckdb.connect()
    con.execute("CREATE TABLE ff(seg_id TEXT, t_ff_s DOUBLE)")
    con.executemany(
        "INSERT INTO ff VALUES (?, ?)",
        [(k, v["t_ff_s"]) for k, v in freeflow["freeflow"].items()],
    )
    con.execute("CREATE TABLE da(date_iso TEXT, pick TEXT, season TEXT, dow INT, weather TEXT, daytype TEXT)")
    con.executemany(
        "INSERT INTO da VALUES (?, ?, ?, ?, ?, ?)",
        [
            (d, a["pick"] or "", a["season"], int(a["dow"]), a["weather"], a["daytype"])
            for d, a in date_attrs["days"].items()
        ],
    )
    con.execute("CREATE TABLE cormap(seg_id TEXT, cid TEXT)")
    con.executemany(
        "INSERT INTO cormap VALUES (?, ?)",
        [
            (s, c["cid"])
            for c in corridors["corridors"]
            for s in c["seg_ids_fwd"] + c["seg_ids_rev"]
        ],
    )

    create_canonical_view(con, glob, registry, city)
    con.execute(
        f"""
        CREATE VIEW t AS
        SELECT tr.seg_id, tr.route_id, tr.period, tr.trip_key,
               strftime(tr.service_date, '%Y-%m-%d') AS date_iso,
               tr.t_obs_s, ff.t_ff_s,
               (tr.t_obs_s - ff.t_ff_s) AS delay_s,
               (tr.t_obs_s / ff.t_ff_s) AS ratio
        FROM trav tr
        JOIN ff ON ff.seg_id = tr.seg_id
        WHERE tr.t_obs_s > 0 AND ff.t_ff_s > 0
          AND tr.max_gap_in_seg_s <= {MAX_GAP_S}
          AND (tr.flags & {FLAG_TOUCHED_TERMINAL}) = 0
        """
    )

    # Total service hours per context are needed for buses/hour; approximate
    # with n_dates × period hours (period=None → 24h-late_night ≈ 18h).
    period_hours = {name: (hi - lo) % 24 for name, lo, hi in city.periods}

    # Entity display names + segment refs.
    seg_label = {k: v["label"] for k, v in registry["segments"].items()}
    cor_name = {c["cid"]: f"{c['name']} ({'+'.join(c['routes'][:4])})" for c in corridors["corridors"]}
    cor_sids = {c["cid"]: c["seg_ids_fwd"] + c["seg_ids_rev"] for c in corridors["corridors"]}

    def label_of(kind: str, eid: str) -> str:
        if kind == "segment":
            return seg_label.get(eid, eid)
        if kind == "corridor":
            return cor_name.get(eid, eid)
        return f"Route {eid}"

    def sids_of(kind: str, eid: str) -> list[str]:
        if kind == "segment":
            return [eid]
        if kind == "corridor":
            return cor_sids.get(eid, [])
        return []  # route: client resolves via registry

    def rank(df, metric: str, hours: float) -> list[dict]:
        vals = df[metric].to_numpy(dtype=float)
        n = df.n.to_numpy(dtype=float)
        z = robust_z(vals)
        zs = shrunk(z, n)
        bph = df.n_trips.to_numpy(dtype=float) / np.maximum(df.n_dates.to_numpy(dtype=float) * hours, 1.0)
        prio = zs * (1.0 + 0.5 * np.log1p(bph))
        out = []
        for i in np.argsort(-np.abs(prio))[:TOP_K]:
            out.append(
                {
                    "eid": df.eid.iloc[int(i)],
                    "value": round(float(vals[i]), 4),
                    "z": round(float(z[i]), 2),
                    "z_shrunk": round(float(zs[i]), 2),
                    "priority": round(float(prio[i]), 2),
                    "n": int(df.n.iloc[int(i)]),
                    "buses_per_hr": round(float(bph[i]), 2),
                }
            )
        return out

    contexts = []

    # ---- LEVEL contexts ---------------------------------------------------
    for daytype, period in LEVEL_FILTERS:
        where = _filter_sql(daytype, period)
        hours = period_hours.get(period, 18.0)
        for kind in ("segment", "corridor", "route"):
            df = con.sql(_metrics_query(kind, where)).df()
            if df.empty:
                continue
            net = {
                m: {"median": float(np.median(df[m])), "mad": float(np.median(np.abs(df[m] - np.median(df[m]))))}
                for m in METRICS
            }
            for metric in METRICS:
                ranked = rank(df, metric, hours)
                if not ranked:
                    continue
                contexts.append(
                    {
                        "id": f"level:{daytype}:{period or 'all'}:{kind}:{metric}",
                        "type": "level",
                        "kind": kind,
                        "metric": metric,
                        "filter": {"daytype": daytype, "period": period},
                        "network": net[metric],
                        "n_entities": len(df),
                        "entities": [
                            {**e, "name": label_of(kind, e["eid"]), "seg_ids": sids_of(kind, e["eid"])[:60]}
                            for e in ranked
                        ],
                    }
                )

    # ---- DIFF contexts ----------------------------------------------------
    for diff_id, (fa, pa), (fb, pb) in DIFF_PAIRS:
        wa, wb = _filter_sql(fa, pa), _filter_sql(fb, pb)
        for kind in ("segment", "corridor", "route"):
            da_ = con.sql(_metrics_query(kind, wa)).df().set_index("eid")
            db_ = con.sql(_metrics_query(kind, wb)).df().set_index("eid")
            common = da_.index.intersection(db_.index)
            if len(common) < 10:
                continue
            for metric in ("mean_delay_ratio", "median_delay_ratio"):
                delta = (db_.loc[common, metric] - da_.loc[common, metric]).to_numpy(dtype=float)
                nh = 2.0 / (1.0 / da_.loc[common, "n"].to_numpy() + 1.0 / db_.loc[common, "n"].to_numpy())
                z = robust_z(delta)
                zs = shrunk(z, nh)
                bph = (
                    db_.loc[common, "n_trips"].to_numpy(dtype=float)
                    / np.maximum(db_.loc[common, "n_dates"].to_numpy(dtype=float) * period_hours.get(pb, 18.0), 1.0)
                )
                prio = zs * (1.0 + 0.5 * np.log1p(bph))
                order = np.argsort(-np.abs(prio))[:TOP_K]
                entities = []
                for i in order:
                    eid = common[int(i)]
                    entities.append(
                        {
                            "eid": eid,
                            "name": label_of(kind, eid),
                            "valueA": round(float(da_.loc[eid, metric]), 4),
                            "valueB": round(float(db_.loc[eid, metric]), 4),
                            "delta": round(float(delta[i]), 4),
                            "z": round(float(z[i]), 2),
                            "z_shrunk": round(float(zs[i]), 2),
                            "priority": round(float(prio[i]), 2),
                            "nA": int(da_.loc[eid, "n"]),
                            "nB": int(db_.loc[eid, "n"]),
                            "buses_per_hr": round(float(bph[i]), 2),
                            "seg_ids": sids_of(kind, eid)[:60],
                        }
                    )
                contexts.append(
                    {
                        "id": f"diff:{diff_id}:{kind}:{metric}",
                        "type": "diff",
                        "kind": kind,
                        "metric": metric,
                        "filterA": {"daytype": fa, "period": pa},
                        "filterB": {"daytype": fb, "period": pb},
                        "network": {
                            "median": float(np.median(delta)),
                            "mad": float(np.median(np.abs(delta - np.median(delta)))),
                        },
                        "n_entities": int(len(common)),
                        "entities": entities,
                    }
                )

    return {
        "meta": {
            "city": city.city_id,
            "intersections_sha256": registry["meta"]["intersections_sha256"],
            "n_contexts": len(contexts),
            "min_n": MIN_N,
            "top_k": TOP_K,
            "shrinkage_n0": N0_SHRINKAGE,
        },
        "contexts": contexts,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--city", default="cta")
    args = ap.parse_args()
    city = get_city(args.city)
    payload = build_areas(city)
    out = REPO / "outputs" / "network" / city.city_id / "areas.json"
    out.write_text(json.dumps(payload))
    print(f"wrote {out}: {payload['meta']['n_contexts']} contexts")


if __name__ == "__main__":
    main()
