"""Per-segment bus-speed effect of heavy rain and of snow.

Each traversal is assigned ONE condition for the clock hour containing its
midpoint (hour-ending convention, matching MRMS):
    snow        the city's airport METAR reports falling snow / snow grains in
                that hour (blowing / drifting snow excluded) — city-wide
    heavy_rain  otherwise, NOAA MRMS gauge-corrected 1-h precipitation in the
                segment-midpoint 0.01° cell >= HEAVY_MM (7.6 mm/h = 0.3 in/h,
                the NWS "heavy rain" rate)
    normal      everything else (dry or lighter rain)

Baseline: the segment's median speed over NORMAL traversals in the same local
hour x day type (weekday | weekend+holiday) x calendar month, falling back to
hour x day type, then period x day type, when the finer slot has fewer than
MIN_BASE normal traversals. Baselines are computed only for slots that hold
heavy-rain or snow traversals (Chicago: 605M traversals).

Effect per segment and condition: median of (speed / baseline - 1) x 100 over
that condition's traversals (% change; negative = slower), plus the median
mph difference. Reported only with >= MIN_N traversals over >= MIN_HOURS
distinct condition hours (traversals in one hour share the weather, so they
are not independent). User decisions 2026-10-05.

Speed = canonical segment length (registry len_m) / traversal time; filters
match the dashboard stats (build_payloads).

Output: outputs/network/<city>/weather_effect.json + dashboard copy
        {"meta": {...}, "segments": {sid: {"heavy_rain": {...}, "snow": {...}}}}

Usage:
    PYTHONPATH=src uv run python analysis/network/weather_effect.py --city cta
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from analysis.network.traversals_view import (  # noqa: E402
    configured_connection, create_canonical_view)
from dataio.cities import get_city  # noqa: E402

HEAVY_MM = 7.6
MIN_BASE = 20
MIN_N = 30
MIN_HOURS = 5
MAX_GAP_S = 180.0   # = build_payloads
FLAG_TOUCHED_TERMINAL = 1
MPS_TO_MPH = 2.23694
COND = {"normal": 0, "heavy_rain": 1, "snow": 2}

IEM = ("https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?station={st}"
       "&data=wxcodes&data=tmpf&year1={y0}&month1={m0}&day1={d0}"
       "&year2={y1}&month2={m1}&day2={d1}&tz=Etc/UTC&format=onlycomma"
       "&latlon=no&missing=M&trace=T&report_type=3&report_type=4")


def midpoint(coords: list) -> tuple[float, float]:
    """(lon, lat) halfway along a lon/lat polyline (equirectangular)."""
    p = np.asarray(coords, dtype=float)
    if len(p) == 1:
        return float(p[0, 0]), float(p[0, 1])
    k = np.cos(np.radians(p[:, 1].mean()))
    seg = np.hypot(np.diff(p[:, 0]) * k, np.diff(p[:, 1]))
    cum = np.r_[0.0, np.cumsum(seg)]
    half = cum[-1] / 2
    i = max(min(int(np.searchsorted(cum, half)) - 1, len(p) - 2), 0)
    f = (half - cum[i]) / seg[i] if seg[i] > 0 else 0.0
    q = p[i] + f * (p[i + 1] - p[i])
    return float(q[0]), float(q[1])


def is_snow(wx) -> bool:
    """METAR present weather -> falling snow? Codes are 2-letter chunks after
    intensity/vicinity; 'SN'/'SG' count unless the descriptor is BL/DR
    (blowing/drifting). Chunking avoids false hits like +TSGRRA (TS+GR+RA)."""
    if not isinstance(wx, str):
        return False
    for tok in wx.split():
        t = tok.lstrip("+-")
        if t.startswith("VC"):
            t = t[2:]
        chunks = [t[i:i + 2] for i in range(0, len(t), 2)]
        if chunks and chunks[0] in ("BL", "DR"):
            continue
        if "SN" in chunks or "SG" in chunks:
            return True
    return False


def snow_hours(city, start: str, end: str) -> np.ndarray:
    """Hour-ending epoch seconds (UTC) with falling snow at the airport."""
    if not city.asos_station:
        return np.array([], dtype=np.int64)
    cache = REPO / "caches" / "weather" / f"{city.asos_station}_asos_wxcodes_{start}_{end}.csv"
    if not cache.exists():
        a = pd.Timestamp(start) - pd.Timedelta(days=1)
        b = pd.Timestamp(end) + pd.Timedelta(days=2)
        url = IEM.format(st=city.asos_station, y0=a.year, m0=a.month, d0=a.day,
                         y1=b.year, m1=b.month, d1=b.day)
        cache.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(url, timeout=600) as r:
            cache.write_bytes(r.read())
    d = pd.read_csv(cache, na_values=["M"])
    d = d[d.wxcodes.map(is_snow)]
    v = pd.to_datetime(d.valid, utc=True)
    he = (v.dt.floor("h") + pd.Timedelta(hours=1)).astype("int64") // 10**9
    return np.unique(he.to_numpy(dtype=np.int64))


def build(city_id: str) -> dict:
    city = get_city(city_id)
    base = REPO / "outputs" / "network" / city.city_id
    reg = json.loads((base / "segment_registry.json").read_text())
    days = json.loads((base / "date_attrs.json").read_text())["days"]
    rain = np.load(REPO / "caches" / "rain" / city.city_id / "mrms_qpe01h_pass2.npz")
    lat, lon, valid, mm = rain["lat"], rain["lon"], rain["valid_utc"], rain["mm"]

    seg_ids = sorted(reg["segments"])
    sid_of = {s: i for i, s in enumerate(seg_ids)}
    cell_of = {}
    for s in seg_ids:
        g = reg["segments"][s].get("geometry_lonlat")
        if g:
            x, y = midpoint(g)
            cell_of[s] = int(np.argmin(np.abs(lat - y))) * len(lon) + int(np.argmin(np.abs(lon - x)))
    cells = np.asarray(sorted(set(cell_of.values())), dtype=np.int64)
    flat = mm.reshape(len(valid), -1)[:, cells]
    hi, ci = np.nonzero(np.nan_to_num(flat) >= HEAVY_MM)       # heavy (hour, cell) pairs only
    heavy = pd.DataFrame({"cell": cells[ci].astype(np.int32), "valid_utc": valid[hi]})
    snow = pd.DataFrame({"valid_utc": snow_hours(city, min(days), max(days))})
    print(f"{city.city_id}: {len(heavy):,} heavy-rain (hour, cell) pairs; "
          f"{len(snow):,} snow hours at {city.asos_station or '-'}", flush=True)

    work = base / "weather_work.duckdb"
    for f in (work, work.with_suffix(".duckdb.wal")):
        f.unlink(missing_ok=True)
    con = configured_connection(city, threads=6, db_path=work)
    con.register("segcell_df", pd.DataFrame(
        {"seg_id": list(cell_of), "sid": [sid_of[s] for s in cell_of],
         "cell": list(cell_of.values()),
         "len_m": [float(reg["segments"][s]["len_m"]) for s in cell_of]}))
    con.execute("CREATE TABLE segcell AS SELECT * FROM segcell_df")
    con.register("heavy_df", heavy)
    con.execute("CREATE TABLE heavy AS SELECT * FROM heavy_df")
    con.register("snow_df", snow)
    con.execute("CREATE TABLE snow AS SELECT * FROM snow_df")
    con.register("dt_df", pd.DataFrame(
        {"d": pd.to_datetime(list(days)).date,
         "wkd": [v["daytype"] == "weekday" for v in days.values()]}))
    con.execute("CREATE TABLE dtype AS SELECT * FROM dt_df")

    glob = str(base / "traversals" / "service_date=*" / "route=*.parquet")
    create_canonical_view(
        con, glob, reg, city,
        pre_filter=(f"tr.t_obs_s > 0 AND tr.max_gap_in_seg_s <= {MAX_GAP_S} "
                    f"AND (tr.flags & {FLAG_TOUCHED_TERMINAL}) = 0"))

    # 1) compact, condition-tagged traversal table (on disk, compressed)
    t0 = time.time()
    con.execute(f"""
        CREATE TABLE tx AS
        WITH b AS (
          SELECT sc.sid, sc.cell, tr.hour_local AS hr, tr.period,
                 dt.wkd, (year(tr.service_date) * 100 + month(tr.service_date))::INT AS ym,
                 (sc.len_m / tr.t_obs_s * {MPS_TO_MPH})::FLOAT AS mph,
                 ((floor(((epoch(tr.t_enter_utc) + epoch(tr.t_exit_utc)) / 2) / 3600) + 1) * 3600)::BIGINT AS he
          FROM trav tr
          JOIN segcell sc ON sc.seg_id = tr.seg_id
          JOIN dtype dt ON dt.d = CAST(tr.service_date AS DATE)
          WHERE tr.t_obs_s > 0 AND tr.max_gap_in_seg_s <= {MAX_GAP_S}
            AND (tr.flags & {FLAG_TOUCHED_TERMINAL}) = 0)
        SELECT b.sid, b.hr, b.period, b.wkd, b.ym, b.mph, b.he,
               (CASE WHEN s.valid_utc IS NOT NULL THEN {COND['snow']}
                     WHEN h.valid_utc IS NOT NULL THEN {COND['heavy_rain']}
                     ELSE {COND['normal']} END)::UTINYINT AS cond
        FROM b
        LEFT JOIN snow s ON s.valid_utc = b.he
        LEFT JOIN heavy h ON h.cell = b.cell AND h.valid_utc = b.he
    """)
    cnt = dict(con.execute("SELECT cond, count(*) FROM tx GROUP BY 1").fetchall())
    name = {v: k for k, v in COND.items()}
    print(f"tagged {sum(cnt.values()):,} traversals: "
          + ", ".join(f"{name[k]} {v:,}" for k, v in sorted(cnt.items()))
          + f" ({time.time() - t0:.0f}s)", flush=True)

    # 2) baselines from NORMAL traversals, only for slots holding condition
    #    traversals; coarser levels only where the finer one is too thin
    t0 = time.time()
    con.execute(f"""
        CREATE TABLE slots AS SELECT DISTINCT sid, hr, wkd, ym, period FROM tx WHERE cond > 0;
        CREATE TABLE b1 AS
          SELECT t.sid, t.hr, t.wkd, t.ym, median(t.mph) med, count(*) n
          FROM tx t SEMI JOIN slots s USING (sid, hr, wkd, ym)
          WHERE t.cond = 0 GROUP BY ALL;
        CREATE TABLE need2 AS
          SELECT DISTINCT s.sid, s.hr, s.wkd FROM slots s
          LEFT JOIN b1 USING (sid, hr, wkd, ym) WHERE coalesce(b1.n, 0) < {MIN_BASE};
        CREATE TABLE b2 AS
          SELECT t.sid, t.hr, t.wkd, median(t.mph) med, count(*) n
          FROM tx t SEMI JOIN need2 USING (sid, hr, wkd)
          WHERE t.cond = 0 GROUP BY ALL;
        CREATE TABLE need3 AS
          SELECT DISTINCT s.sid, s.period, s.wkd FROM slots s
          LEFT JOIN b1 USING (sid, hr, wkd, ym)
          LEFT JOIN b2 USING (sid, hr, wkd)
          WHERE coalesce(b1.n, 0) < {MIN_BASE} AND coalesce(b2.n, 0) < {MIN_BASE};
        CREATE TABLE b3 AS
          SELECT t.sid, t.period, t.wkd, median(t.mph) med, count(*) n
          FROM tx t SEMI JOIN need3 USING (sid, period, wkd)
          WHERE t.cond = 0 GROUP BY ALL;
    """)
    print(f"baselines: {con.execute('SELECT count(*) FROM slots').fetchone()[0]:,} slots, "
          f"fallbacks {con.execute('SELECT count(*) FROM need2').fetchone()[0]:,} / "
          f"{con.execute('SELECT count(*) FROM need3').fetchone()[0]:,} "
          f"({time.time() - t0:.0f}s)", flush=True)

    # 3) per-segment effects
    t0 = time.time()
    rows = con.execute(f"""
        WITH c AS (
          SELECT t.sid, t.cond, t.he, t.mph,
                 CASE WHEN b1.n >= {MIN_BASE} THEN b1.med
                      WHEN b2.n >= {MIN_BASE} THEN b2.med
                      WHEN b3.n >= {MIN_BASE} THEN b3.med END AS base
          FROM tx t
          LEFT JOIN b1 USING (sid, hr, wkd, ym)
          LEFT JOIN b2 USING (sid, hr, wkd)
          LEFT JOIN b3 USING (sid, period, wkd)
          WHERE t.cond > 0)
        SELECT sid, cond, count(*) n, count(DISTINCT he) n_hours,  -- "hours" is reserved
               median((mph / base - 1) * 100) pct, median(mph - base) dmph,
               median(base) base_mph, median(mph) cond_mph
        FROM c WHERE base > 0 GROUP BY ALL
    """).fetchall()
    print(f"effects for {len(rows):,} (segment, condition) pairs ({time.time() - t0:.0f}s)", flush=True)

    segs: dict[str, dict] = {}
    for sid, cond, n, hours, pct, dmph, bm, cm in rows:
        if n < MIN_N or hours < MIN_HOURS:
            continue
        segs.setdefault(str(int(sid)), {})[name[int(cond)]] = {
            "pct": round(float(pct), 2), "dmph": round(float(dmph), 2),
            "n": int(n), "hours": int(hours),
            "base_mph": round(float(bm), 2), "cond_mph": round(float(cm), 2),
        }
    con.close()
    for f in (work, work.with_suffix(".duckdb.wal")):
        f.unlink(missing_ok=True)

    out = {
        "meta": {
            "city": city.city_id, "dates": [min(days), max(days)],
            "heavy_rain": f"MRMS MultiSensor_QPE_01H_Pass2 >= {HEAVY_MM} mm in the segment-midpoint 0.01° cell",
            "snow": f"{city.asos_station} METAR falling snow (SN/SG, not BL/DR) in the hour, city-wide",
            "baseline": "median normal-hour speed: hour x daytype x month, fallback hour x daytype, then period x daytype",
            "effect": "median % speed change vs baseline (negative = slower)",
            "min_n": MIN_N, "min_hours": MIN_HOURS, "min_base": MIN_BASE,
            "traversals": {name[k]: int(v) for k, v in cnt.items()},
            "intersections_sha256": reg["meta"]["intersections_sha256"],
        },
        "segments": segs,
    }
    (base / "weather_effect.json").write_text(json.dumps(out, separators=(",", ":")))
    dash = REPO / "dashboard" / "data" / "network"
    dash = dash if city.city_id == "cta" else dash / city.city_id
    (dash / "weather_effect.json").write_text(json.dumps(out, separators=(",", ":")))
    for c in ("heavy_rain", "snow"):
        v = np.array([s[c]["pct"] for s in segs.values() if c in s])
        if len(v):
            print(f"{c}: {len(v):,} segments | median {np.median(v):+.1f}% "
                  f"(p10 {np.percentile(v, 10):+.1f}, p90 {np.percentile(v, 90):+.1f})")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--city", required=True)
    a = ap.parse_args()
    build(a.city)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
