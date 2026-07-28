"""Build the dashboard's network payloads from traversals + registries.

Outputs (dashboard/data/network/):
    meta.json          dim encodings, hist edges, period defs, date-count table
    segments.json      GeoJSON FeatureCollection (one feature per segment)
    corridors.json     corridor entities with segment index refs
    stats_<period>.bin packed columnar per-bin stats, one shard per period
    golden.json        parity fixture for the JS decoder

Bin key: (seg, route, pick, season, dow, weather) × period(shard). Per bin:
n, sum_delay, m2, and a 16-bucket histogram of delay ratio t_obs/t_ff (see
``stats.py``). Aggregation runs in duckdb straight off the parquet glob.

Binary shard layout (little-endian):
    8s   magic  b"NWSTATS1"
    u32  header_len
    bytes JSON header {n_rows, columns: [{name, dtype}...]}
    then contiguous per-column arrays in header order.

Usage:
    PYTHONPATH=src uv run python analysis/network/build_payloads.py --city cta
"""

from __future__ import annotations

import argparse
import gzip
import json
import struct
import sys
from pathlib import Path

import duckdb
import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from analysis.network.traversals_view import create_canonical_view  # noqa: E402
from analysis.network.stats import (  # noqa: E402
    HIST_EDGES,
    N_BUCKETS,
    OVER_EDGE,
    UNDER_EDGE,
    emit_golden_vectors,
)
from dataio.cities import CityConfig, get_city  # noqa: E402

MAGIC = b"NWSTATS1"
MAX_SHARD_MB = 24  # Cloudflare Pages per-file limit is 25 MB

# Aggregation-time traversal filters.
MAX_GAP_S = 180.0
FLAG_TOUCHED_TERMINAL = 1

# dow encoding 0-6 = Mon..Sun; 7 = holiday (excluded from "weekday" per spec)
DOW_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun", "holiday"]
WEATHER_NAMES = ["dry", "rain", "snow", "unknown"]


def _dims(city: CityConfig, registry: dict, date_attrs: dict) -> dict:
    seg_ids = sorted(registry["segments"])
    route_ids = sorted({r["route_id"] for s in registry["segments"].values() for r in s["routes"]})
    picks = [p.pick_id for p in city.picks]
    seasons = sorted({d["season"] for d in date_attrs["days"].values()})
    periods = [name for name, _, _ in city.periods]
    if len(route_ids) > 255 or len(seg_ids) > 65535:
        raise SystemExit("dimension overflow: widen route/seg dtypes")
    return {
        "seg_ids": seg_ids,
        "route_ids": route_ids,
        "picks": picks,
        "seasons": seasons,
        "dows": DOW_NAMES,
        "weathers": WEATHER_NAMES,
        "periods": periods,
    }


def _aggregate(
    city: CityConfig,
    traversals_glob: str,
    freeflow: dict,
    date_attrs: dict,
    dims: dict,
    registry: dict,
) -> "duckdb.DuckDBPyRelation":
    """One duckdb query: filter, join attrs + freeflow, bin, histogram."""
    con = duckdb.connect()
    sidecar = str(
        REPO / "outputs" / "network" / city.city_id / "door_sidecar" / "service_date=*.parquet"
    )
    create_canonical_view(con, traversals_glob, registry, city, door_sidecar_glob=sidecar)

    # Lookup tables.
    ff_rows = [(k, v["t_ff_s"]) for k, v in freeflow["freeflow"].items()]
    con.execute("CREATE TABLE ff(seg_id TEXT, t_ff_s DOUBLE)")
    con.executemany("INSERT INTO ff VALUES (?, ?)", ff_rows)

    da_rows = [
        (d, a["pick"] or "", a["season"], int(a["dow"]), a["weather"], a["daytype"])
        for d, a in date_attrs["days"].items()
    ]
    con.execute(
        "CREATE TABLE da(date_iso TEXT, pick TEXT, season TEXT, dow INT, weather TEXT, daytype TEXT)"
    )
    con.executemany("INSERT INTO da VALUES (?, ?, ?, ?, ?, ?)", da_rows)

    def enc(col: str, names: list[str]) -> str:
        w = " ".join(
            f"WHEN {col} = '{v}' THEN {i}" for i, v in enumerate(names)
        )
        return f"CASE {w} ELSE {len(names) - 1} END"

    # Histogram bucket counts as 16 conditional sums over the ratio.
    lo = [UNDER_EDGE] + list(HIST_EDGES)
    bucket_cols = []
    for b in range(N_BUCKETS):
        if b == 0:
            cond = f"ratio < {HIST_EDGES[0]}"
        elif b == N_BUCKETS - 1:
            cond = f"ratio >= {HIST_EDGES[-1]}"
        else:
            cond = f"ratio >= {HIST_EDGES[b-1]} AND ratio < {HIST_EDGES[b]}"
        bucket_cols.append(f"SUM(CASE WHEN {cond} THEN 1 ELSE 0 END)::BIGINT AS h{b}")

    seg_enc = {s: i for i, s in enumerate(dims["seg_ids"])}
    con.execute("CREATE TABLE segenc(seg_id TEXT, sid INT)")
    con.executemany("INSERT INTO segenc VALUES (?, ?)", list(seg_enc.items()))
    route_enc = {r: i for i, r in enumerate(dims["route_ids"])}
    con.execute("CREATE TABLE routeenc(route_id TEXT, rid INT)")
    con.executemany("INSERT INTO routeenc VALUES (?, ?)", list(route_enc.items()))

    q = f"""
    WITH t AS (
      SELECT
        tr.seg_id, tr.route_id, tr.period,
        strftime(tr.service_date, '%Y-%m-%d') AS date_iso,
        tr.t_obs_s, ff.t_ff_s,
        (tr.t_obs_s - ff.t_ff_s) AS delay_s,
        (tr.t_obs_s / ff.t_ff_s) AS ratio,
        tr.has_door, tr.dwell_s, tr.ons, tr.offs, tr.load_sum
      FROM trav tr
      JOIN ff ON ff.seg_id = tr.seg_id
      WHERE tr.t_obs_s > 0
        AND tr.max_gap_in_seg_s <= {MAX_GAP_S}
        AND (tr.flags & {FLAG_TOUCHED_TERMINAL}) = 0
        AND ff.t_ff_s > 0
    )
    SELECT
      se.sid, re.rid,
      {enc('da.pick', dims['picks'])} AS pick,
      {enc('da.season', dims['seasons'])} AS season,
      CASE WHEN da.daytype = 'holiday' THEN 7 ELSE da.dow END AS dow,
      {enc('da.weather', dims['weathers'])} AS weather,
      t.period AS period,
      COUNT(*)::BIGINT AS n,
      SUM(t.delay_s) AS sum_delay,
      COALESCE(VAR_POP(t.delay_s) * COUNT(*), 0.0) AS m2,
      COUNT(*) FILTER (WHERE t.has_door)::BIGINT AS n_door,
      COALESCE(SUM(t.dwell_s)  FILTER (WHERE t.has_door), 0.0) AS sum_dwell,
      COALESCE(SUM(t.delay_s)  FILTER (WHERE t.has_door), 0.0) AS sum_delay_door,
      COALESCE(SUM(t.ons)      FILTER (WHERE t.has_door), 0)   AS sum_ons,
      COALESCE(SUM(t.offs)     FILTER (WHERE t.has_door), 0)   AS sum_offs,
      COALESCE(SUM(t.load_sum) FILTER (WHERE t.has_door), 0)   AS sum_load,
      {', '.join(bucket_cols)}
    FROM t
    JOIN da ON da.date_iso = t.date_iso
    JOIN segenc se ON se.seg_id = t.seg_id
    JOIN routeenc re ON re.route_id = t.route_id
    GROUP BY 1, 2, 3, 4, 5, 6, 7
    """
    return con, con.sql(q)


def _write_shard(path: Path, rows: dict[str, np.ndarray]) -> int:
    """Write one packed columnar shard; returns byte size."""
    dtypes = {
        "sid": "<u2", "rid": "<u1", "pick": "<u1", "season": "<u1",
        "dow": "<u1", "weather": "<u1",
        "n": "<u2", "sum_delay": "<f4", "m2": "<f4",
        # door/APC (zero when the bin's dates precede door coverage);
        # sum_load stays un-surfaced in the UI for now, by design.
        "n_door": "<u2", "sum_dwell": "<f4", "sum_delay_door": "<f4",
        "sum_ons": "<f4", "sum_offs": "<f4", "sum_load": "<f4",
        **{f"h{b}": "<u2" for b in range(N_BUCKETS)},
    }
    n_rows = len(rows["sid"])
    header = {
        "n_rows": n_rows,
        "columns": [{"name": k, "dtype": v} for k, v in dtypes.items()],
    }
    hjson = json.dumps(header).encode()
    buf = bytearray()
    buf += MAGIC
    buf += struct.pack("<I", len(hjson))
    buf += hjson
    for name, dt in dtypes.items():
        arr = rows[name]
        if dt == "<u2":
            arr = np.minimum(arr, 65535)
        elif dt == "<u1":
            arr = np.minimum(arr, 255)
        buf += np.ascontiguousarray(arr.astype(dt)).tobytes()
    path.write_bytes(bytes(buf))
    # Pre-compressed twin: Cloudflare Pages doesn't compress octet-stream, so
    # the client fetches .bin.gz and inflates via DecompressionStream (with a
    # raw-.bin fallback for older browsers / missing twin).
    path.with_suffix(".bin.gz").write_bytes(gzip.compress(bytes(buf), 9))
    return len(buf)


def build(city_id: str, out_dir: Path | None = None) -> None:
    city = get_city(city_id)
    base = REPO / "outputs" / "network" / city.city_id
    out = out_dir or REPO / "dashboard" / "data" / "network"
    out.mkdir(parents=True, exist_ok=True)

    registry = json.loads((base / "segment_registry.json").read_text())
    corridors = json.loads((base / "corridors.json").read_text())
    freeflow = json.loads((base / "freeflow.json").read_text())
    date_attrs = json.loads((base / "date_attrs.json").read_text())

    sha = registry["meta"]["intersections_sha256"]
    for name, payload in (("freeflow", freeflow), ("corridors", corridors)):
        if payload["meta"].get("intersections_sha256") not in (None, sha):
            raise SystemExit(f"{name}.json built against a different intersections cache")

    dims = _dims(city, registry, date_attrs)
    seg_index = {s: i for i, s in enumerate(dims["seg_ids"])}

    # ---- stats shards, one per period ------------------------------------
    glob = str(base / "traversals" / "service_date=*" / "route=*.parquet")
    sidecar = str(base / "door_sidecar" / "service_date=*.parquet")
    con, rel = _aggregate(city, glob, freeflow, date_attrs, dims, registry)
    df = rel.df()
    print(f"binned rows total: {len(df)}")

    shard_meta = {}
    for pi, period in enumerate(dims["periods"]):
        sub = df[df.period == period]
        cols = {
            "sid": sub.sid.to_numpy(),
            "rid": sub.rid.to_numpy(),
            "pick": sub.pick.to_numpy(),
            "season": sub.season.to_numpy(),
            "dow": sub.dow.to_numpy(),
            "weather": sub.weather.to_numpy(),
            "n": sub.n.to_numpy(),
            "sum_delay": sub.sum_delay.to_numpy(),
            "m2": sub.m2.to_numpy(),
            "n_door": sub.n_door.to_numpy(),
            "sum_dwell": sub.sum_dwell.to_numpy(),
            "sum_delay_door": sub.sum_delay_door.to_numpy(),
            "sum_ons": sub.sum_ons.to_numpy(),
            "sum_offs": sub.sum_offs.to_numpy(),
            "sum_load": sub.sum_load.to_numpy(),
            **{f"h{b}": sub[f"h{b}"].to_numpy() for b in range(N_BUCKETS)},
        }
        path = out / f"stats_{period}.bin"
        size = _write_shard(path, cols)
        mb = size / 1e6
        assert mb < MAX_SHARD_MB, f"{path.name} is {mb:.1f} MB — split needed"
        shard_meta[period] = {"rows": len(sub), "bytes": size}
        print(f"  {path.name}: {len(sub)} rows, {mb:.2f} MB")

    # ---- segments.json (GeoJSON) -----------------------------------------
    seg_corridors: dict[str, list[str]] = {}
    for c in corridors["corridors"]:
        for s in c["seg_ids_fwd"] + c["seg_ids_rev"]:
            seg_corridors.setdefault(s, []).append(c["cid"])

    features = []
    for seg_id in dims["seg_ids"]:
        rec = registry["segments"][seg_id]
        ff = freeflow["freeflow"].get(seg_id, {})
        features.append(
            {
                "type": "Feature",
                "id": seg_index[seg_id],
                "geometry": {"type": "LineString", "coordinates": rec["geometry_lonlat"]},
                "properties": {
                    "sid": seg_index[seg_id],
                    "seg_id": seg_id,
                    "label": rec["label"],
                    "name": rec["name"],
                    "len_m": rec["len_m"],
                    "t_ff_s": ff.get("t_ff_s"),
                    "ff_method": ff.get("method"),
                    "routes": [
                        {"r": r["route_id"], "dir": r["direction"]} for r in rec["routes"]
                    ],
                    "rev_sid": seg_index.get(rec["rev_seg_id"]),
                    "corridors": seg_corridors.get(seg_id, []),
                    "n_stops": rec["n_stops"],
                },
            }
        )
    (out / "segments.json").write_text(
        json.dumps({"type": "FeatureCollection", "features": features})
    )

    # ---- corridors.json ---------------------------------------------------
    cor_out = {
        "meta": corridors["meta"],
        "corridors": [
            {
                **{k: c[k] for k in ("cid", "name", "routes", "len_m", "dir_fwd", "dir_rev")},
                "sids_fwd": [seg_index[s] for s in c["seg_ids_fwd"] if s in seg_index],
                "sids_rev": [seg_index[s] for s in c["seg_ids_rev"] if s in seg_index],
            }
            for c in corridors["corridors"]
        ],
    }
    (out / "corridors.json").write_text(json.dumps(cor_out))

    # ---- meta.json --------------------------------------------------------
    # Date counts per (pick, season, dow, weather) — only dates that actually
    # produced traversals, so buses/hour denominators are honest.
    dates_present = {
        r[0]
        for r in con.execute(
            "SELECT DISTINCT strftime(service_date, '%Y-%m-%d') FROM trav"
        ).fetchall()
    }
    date_counts: dict[str, int] = {}
    for d, a in date_attrs["days"].items():
        if d not in dates_present:
            continue
        key = "|".join(
            [
                str(dims["picks"].index(a["pick"]) if a["pick"] in dims["picks"] else 0),
                str(dims["seasons"].index(a["season"])),
                str(7 if a["daytype"] == "holiday" else a["dow"]),
                str(
                    dims["weathers"].index(a["weather"])
                    if a["weather"] in dims["weathers"]
                    else dims["weathers"].index("unknown")
                ),
            ]
        )
        date_counts[key] = date_counts.get(key, 0) + 1

    # Door-covered service dates (sidecar files present) — the boardings/hour
    # denominator uses these, not all dates.
    import glob as _globmod
    door_dates = {
        Path(f).stem.split("=")[1]
        for f in _globmod.glob(sidecar)
    }
    door_date_counts: dict[str, int] = {}
    for d, a in date_attrs["days"].items():
        if d not in dates_present or d not in door_dates:
            continue
        key = "|".join(
            [
                str(dims["picks"].index(a["pick"]) if a["pick"] in dims["picks"] else 0),
                str(dims["seasons"].index(a["season"])),
                str(7 if a["daytype"] == "holiday" else a["dow"]),
                str(
                    dims["weathers"].index(a["weather"])
                    if a["weather"] in dims["weathers"]
                    else dims["weathers"].index("unknown")
                ),
            ]
        )
        door_date_counts[key] = door_date_counts.get(key, 0) + 1

    meta = {
        "city": city.city_id,
        "intersections_sha256": sha,
        "dims": dims,
        "period_hours": {name: (hi - lo) % 24 for name, lo, hi in city.periods},
        "hist_edges": list(HIST_EDGES),
        "under_edge": UNDER_EDGE,
        "over_edge": OVER_EDGE,
        "date_counts": date_counts,
        "door_date_counts": door_date_counts,
        "n_dates": len(dates_present),
        "n_door_dates": len(door_dates & dates_present),
        "shards": shard_meta,
        "daytype_by_dow": {str(i): ("weekday" if i < 5 else DOW_NAMES[i]) for i in range(7)},
        "holidays_excluded_from_weekday": True,
    }
    (out / "meta.json").write_text(json.dumps(meta))

    emit_golden_vectors(out / "golden.json")
    print(f"wrote payloads to {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--city", default="cta")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    build(args.city, Path(args.out) if args.out else None)


if __name__ == "__main__":
    main()
