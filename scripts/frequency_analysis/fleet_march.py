"""Fleet-wide AVL evaluation, March 2026 (all CTA vehicles).

Per day: segment each bus's archive rows into trips by (trip_id,
pattern_id) runs; map pattern -> GTFS shape via the 5-digit suffix
convention; map-match + clean; QC (revenue route, >=30 pings, 5 min-3 h,
>=3 km forward, >=70% on-route, no internal gap > 120 s). Score:

  M1/M2  paper-literal 5% self-holdout (PCHIP + VCHIP-ME, speeds ft/s)
  M3     doors-open stopped % vs the monthly bus-state parquet (types 3/5)

A deterministic ~0.5% of trips additionally runs the LOCREG grids
(LOCREG-PCHIP k, LOCREG-PCHIP-V kx/kv) on M1/M2/M3 for the degeneracy
check. Writes results/fleet_march_trips.csv (+ _locreg.csv).

    uv run python fleet_march.py            # ~20-40 min on 8 workers
"""

from __future__ import annotations

import io
import itertools
import zipfile
import zlib
from multiprocessing import Pool
from pathlib import Path

import numpy as np
import pandas as pd

import config as C
import methods as M
from trip_index import clean_dist_along

AVL_DIR = Path("/Users/ashwinagarwal/Library/CloudStorage/"
               "OneDrive-ChicagoTransitAuthority/CTA AVL Archive/avl_archive")
DOOR_MONTH = Path("/Users/ashwinagarwal/Library/CloudStorage/"
                  "OneDrive-ChicagoTransitAuthority/Bus State History/"
                  "bus_state_hist/month=202603.parquet")
DAYS = [f"2026-03-{d:02d}" for d in range(1, 32)]
FT = 0.3048
DOOR_TYPES = {3, 5}
LOCREG_SAMPLE_MOD = 200        # ~0.5% of trips run the LOCREG grids

MAX_GAP_S = 120.0
MIN_PINGS = 30
MIN_DUR_S, MAX_DUR_S = 300.0, 3 * 3600.0


def is_revenue(route_id: str) -> bool:
    r = str(route_id).strip()
    return (r not in {"", "0", "992", "999", "None", "<NA>"}
            and not r.startswith(("PI", "PO", "DH")))


def shape_suffix_map() -> dict[str, str]:
    with zipfile.ZipFile(C.GTFS) as z:
        trips = pd.read_csv(io.BytesIO(z.read("trips.txt")),
                            usecols=["shape_id"])
    out: dict[str, str] = {}
    for s in trips["shape_id"].astype(str).unique():
        out.setdefault(s[-5:], s)
    return out


def prep_doors() -> Path:
    """Split the monthly door parquet into per-day files (once)."""
    out_dir = C.CACHE_DIR / "doors_202603"
    if out_dir.exists() and len(list(out_dir.glob("*.parquet"))) >= 28:
        return out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    df = pd.read_parquet(DOOR_MONTH, columns=[
        "bus_id", "event_time", "event_type", "dwell_time"])
    df = df[df["event_type"].isin(DOOR_TYPES)]
    df["dwell_s"] = pd.to_numeric(df["dwell_time"], errors="coerce")
    df = df[(df["dwell_s"] >= C.MIN_DOOR_DWELL_S)
            & (df["dwell_s"] <= C.MAX_DOOR_DWELL_S)]
    df["day"] = df["event_time"].dt.strftime("%Y-%m-%d")
    for day, g in df.groupby("day"):
        g[["bus_id", "event_time", "dwell_s"]].to_parquet(
            out_dir / f"{day}.parquet")
    return out_dir


_SUFFIX: dict[str, str] = {}
_DOOR_DIR: Path | None = None


def _init(suffix_map, door_dir):
    global _SUFFIX, _DOOR_DIR
    _SUFFIX, _DOOR_DIR = suffix_map, door_dir


def _self_holdout(key, t, x, v, method, params=None):
    rng = np.random.default_rng((C.HOLDOUT_SEED, zlib.crc32(key.encode())))
    held = np.zeros(len(t), dtype=bool)
    held[rng.choice(len(t), max(1, int(round(len(t) * C.HOLDOUT_FRACTION))),
                    replace=False)] = True
    tk, xk, vk = t[~held], x[~held], v[~held]
    inside = held & (t >= tk[0]) & (t <= tk[-1])
    if inside.sum() < 1 or (~held).sum() < 4:
        return None
    H = M.BUILDERS[method](tk, xk, vk, **(params or {}))
    ex = H.pos(t[inside]) - x[inside]
    ev = H.vel(t[inside]) - v[inside]
    return (float(np.mean(np.abs(ex))), float(np.mean(np.abs(ev))),
            int(inside.sum()))


def _door_m3(t, x, v, method, door_t0, door_dw, params=None):
    """% of door-open seconds with reconstructed speed < 5 ft/s (+2/10)."""
    if not len(door_t0):
        return {}
    H = M.BUILDERS[method](t, x, v, **(params or {}))
    grid = np.arange(np.ceil(t[0]), np.floor(t[-1]) + 0.5, 1.0)
    if len(grid) < 30:
        return {}
    v_fps = H.vel(grid) / FT
    masks = [(grid >= s) & (grid < s + d) for s, d in zip(door_t0, door_dw)]
    tot = int(sum(m.sum() for m in masks))
    if not tot:
        return {}
    out = {"door_total_s": tot}
    for th in (2.0, 5.0, 10.0):
        out[f"door_stop_{th}"] = int(sum((v_fps[m] < th).sum() for m in masks))
    return out


def run_day(day: str) -> tuple[list[dict], list[dict]]:
    try:
        df = pd.read_parquet(AVL_DIR / f"date={day}.parquet", columns=[
            "bus_id", "avl_event_time", "trip_id", "route_id", "pattern_id",
            "speed", "latitude", "longitude"])
    except FileNotFoundError:
        return [], []
    df = df[df["route_id"].astype(str).map(is_revenue)
            & df["pattern_id"].notna() & df["trip_id"].notna()]
    df = (df.drop_duplicates(["bus_id", "avl_event_time"])
            .sort_values(["bus_id", "avl_event_time"]))
    doors = pd.read_parquet(_DOOR_DIR / f"{day}.parquet")
    doors_by_bus = dict(tuple(doors.groupby("bus_id")))

    from analysis.comparison import matcher_for
    matchers: dict[str, object] = {}
    rows, locreg_rows = [], []

    for bus, g in df.groupby("bus_id"):
        g = g.reset_index(drop=True)
        t_all = g["avl_event_time"].astype("datetime64[ns]")
        gap = t_all.diff().dt.total_seconds().fillna(0)
        run_id = ((g["trip_id"] != g["trip_id"].shift())
                  | (g["pattern_id"] != g["pattern_id"].shift())
                  | (gap > 600)).cumsum()
        for _, tr in g.groupby(run_id):
            if len(tr) < MIN_PINGS:
                continue
            ts = tr["avl_event_time"].astype("datetime64[ns]")
            dur = (ts.iloc[-1] - ts.iloc[0]).total_seconds()
            if not (MIN_DUR_S <= dur <= MAX_DUR_S):
                continue
            t = ts.astype("int64").to_numpy() / 1e9
            if np.max(np.diff(t)) > MAX_GAP_S:
                continue
            shape = _SUFFIX.get(f"{int(tr['pattern_id'].iloc[0]):05d}")
            if shape is None:
                continue
            if shape not in matchers:
                try:
                    matchers[shape] = matcher_for(shape)
                except KeyError:
                    matchers[shape] = None
            mm = matchers[shape]
            if mm is None:
                continue
            res = mm.match(tr["latitude"].to_numpy(),
                           tr["longitude"].to_numpy())
            if res.on_route.mean() < C.MIN_ON_ROUTE_FRAC:
                continue
            xc, keep = clean_dist_along(t, res.dist_along_m, res.on_route)
            if keep.sum() < MIN_PINGS or xc[-1] - xc[0] < C.MIN_FORWARD_M:
                continue
            tt = t[keep]
            if np.max(np.diff(tt)) > MAX_GAP_S:
                continue
            vv = (pd.to_numeric(tr["speed"], errors="coerce")
                  .fillna(0.0).to_numpy()[keep] * FT)
            key = f"{bus}_{int(tr['trip_id'].iloc[0])}_{day}_{ts.iloc[0]:%H%M%S}"
            cadence = float(np.median(np.diff(tt)))

            d = doors_by_bus.get(bus)
            if d is not None:
                sel = ((d["event_time"] >= ts.iloc[0])
                       & (d["event_time"] <= ts.iloc[-1]))
                door_t0 = d.loc[sel, "event_time"].astype(
                    "datetime64[ns]").astype("int64").to_numpy() / 1e9
                door_dw = d.loc[sel, "dwell_s"].to_numpy()
            else:
                door_t0 = door_dw = np.array([])

            row = {"trip_key": key, "day": day, "bus_id": int(bus),
                   "route_id": str(tr["route_id"].iloc[0]),
                   "cadence_s": cadence, "n_pings": int(keep.sum()),
                   "dur_s": dur}
            ok = True
            for method in ("PCHIP", "VCHIP-ME"):
                ho = _self_holdout(key, tt, xc, vv, method)
                if ho is None:
                    ok = False
                    break
                m3 = _door_m3(tt, xc, vv, method, door_t0, door_dw)
                tag = "p" if method == "PCHIP" else "v"
                row[f"ho_mae_x_{tag}"], row[f"ho_mae_v_{tag}"], row["ho_n"] = ho
                for kk, vvv in m3.items():
                    row[f"{kk}_{tag}"] = vvv
            if not ok:
                continue
            rows.append(row)

            if zlib.crc32(key.encode()) % LOCREG_SAMPLE_MOD == 0:
                for k in C.K_GRID:
                    ho = _self_holdout(key, tt, xc, vv, "LOCREG-PCHIP",
                                       {"k": k})
                    m3 = _door_m3(tt, xc, vv, "LOCREG-PCHIP", door_t0,
                                  door_dw, {"k": k})
                    if ho:
                        locreg_rows.append({
                            "trip_key": key, "method": "LOCREG-PCHIP",
                            "params": f"k={k}", "ho_mae_x": ho[0],
                            "ho_mae_v": ho[1], **m3})
                for kx, kv in itertools.product(C.KXV_GRID, C.KXV_GRID):
                    ho = _self_holdout(key, tt, xc, vv, "LOCREG-PCHIP-V",
                                       {"kx": kx, "kv": kv})
                    m3 = _door_m3(tt, xc, vv, "LOCREG-PCHIP-V", door_t0,
                                  door_dw, {"kx": kx, "kv": kv})
                    if ho:
                        locreg_rows.append({
                            "trip_key": key, "method": "LOCREG-PCHIP-V",
                            "params": f"kx={kx},kv={kv}", "ho_mae_x": ho[0],
                            "ho_mae_v": ho[1], **m3})
    return rows, locreg_rows


def main():
    suffix = shape_suffix_map()
    door_dir = prep_doors()
    all_rows, all_locreg = [], []
    with Pool(processes=8, initializer=_init,
              initargs=(suffix, door_dir)) as pool:
        for i, (rows, lr) in enumerate(pool.imap_unordered(run_day, DAYS)):
            all_rows += rows
            all_locreg += lr
            print(f"  day done ({i + 1}/{len(DAYS)}): +{len(rows)} trips "
                  f"(total {len(all_rows)})", flush=True)
    df = pd.DataFrame(all_rows)
    df.to_csv(C.RESULTS_DIR / "fleet_march_trips.csv", index=False)
    pd.DataFrame(all_locreg).to_csv(
        C.RESULTS_DIR / "fleet_march_locreg.csv", index=False)

    print(f"\n{len(df)} trips, {df.bus_id.nunique()} buses, "
          f"{df.route_id.nunique()} routes")
    print(f"cadence: median {df.cadence_s.median():.1f}s "
          f"IQR [{df.cadence_s.quantile(.25):.1f}, "
          f"{df.cadence_s.quantile(.75):.1f}]")
    for tag, name in (("p", "PCHIP"), ("v", "VCHIP-ME")):
        tot = df[f"door_total_s_{tag}"].sum()
        print(f"  {name:9s} M1={df[f'ho_mae_x_{tag}'].mean():6.2f} m  "
              f"M2={df[f'ho_mae_v_{tag}'].mean() / FT:5.2f} ft/s  "
              f"M3={100 * df[f'door_stop_5.0_{tag}'].sum() / tot:5.1f}%")


if __name__ == "__main__":
    main()
