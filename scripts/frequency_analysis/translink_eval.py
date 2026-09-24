"""TransLink R99 (Vancouver) reference: 1000-trip sample, PCHIP only.

The cached GTFS-rt archive (`caches/translink_r99_all.parquet`, Jun 23 -
Jul 28 2026, deduped cadence ~29 s) has positions only — no speed channel,
no door events — so the point is scored on M1 (5% self-holdout position
MAE) and M4a/M4b (acceleration realism); M2/M3 are not computable.

    uv run python translink_eval.py   # -> results/translink_reference.csv
"""

from __future__ import annotations

import io
import zipfile
import zlib

import numpy as np
import pandas as pd

import config as C
import methods as M
from trip_index import clean_dist_along

TL_CACHE = C.REPO / "caches" / "translink_r99_all.parquet"
TL_GTFS = C.REPO / "data" / "gtfs" / "translink_gtfs.zip"
TL_ARCHIVE = C.REPO / "caches" / "realtime_archive"
N_SAMPLE = 1000
SEED = 20260826


def load_route_from_archive(route_id: str, tag: str) -> pd.DataFrame:
    """Assemble a per-route ping frame from the hourly TransLink archive."""
    cache = C.CACHE_DIR / f"translink_{tag}_all.parquet"
    if cache.exists():
        return pd.read_parquet(cache)
    frames = []
    for f in sorted(TL_ARCHIVE.glob("agency=translink__*.parquet")):
        df = pd.read_parquet(f, columns=[
            "vehicle_id", "trip_id", "route_id", "start_date", "timestamp",
            "latitude", "longitude"])
        df = df[df["route_id"].astype(str) == route_id]
        if len(df):
            frames.append(df.drop(columns="route_id"))
    out = pd.concat(frames, ignore_index=True)
    out.to_parquet(cache)
    return out


def matchers_by_trip():
    from dataio.gtfs import load_gtfs_shape_with_dist
    from core.mapmatch import get_matcher
    with zipfile.ZipFile(TL_GTFS) as z:
        trips = pd.read_csv(io.BytesIO(z.read("trips.txt")),
                            usecols=["trip_id", "shape_id"], dtype=str)
    trip2shape = dict(zip(trips.trip_id, trips.shape_id))
    cache = {}

    def get(trip_id: str):
        shape = trip2shape.get(trip_id)
        if shape is None:
            return None
        if shape not in cache:
            poly, _dist = load_gtfs_shape_with_dist(TL_GTFS, shape)
            # TransLink's shape_dist_traveled is in unusable units (5.17 for
            # a 16.9 km shape) -> let the matcher build its own geodesic
            # meters ruler from the polyline
            cache[shape] = get_matcher("shape_snap", polyline_latlon=poly,
                                       max_perp_m=50.0)
        return cache[shape]
    return get


def main(route: str = "R99"):
    if route == "R99":
        df = pd.read_parquet(TL_CACHE, columns=[
            "vehicle_id", "trip_id", "start_date", "timestamp",
            "latitude", "longitude"])
        out_csv = "translink_reference.csv"
    else:
        df = load_route_from_archive({"R4": "37810"}[route], route.lower())
        out_csv = f"translink_{route.lower()}_reference.csv"
    df = (df.drop_duplicates(["vehicle_id", "timestamp"])
            .sort_values(["vehicle_id", "timestamp"]))
    df["trip_id"] = df["trip_id"].astype(str)
    get_matcher_for = matchers_by_trip()

    insts = [(k, g) for k, g in df.groupby(["trip_id", "start_date"])
             if len(g) >= 30 and g["vehicle_id"].nunique() == 1]
    rng = np.random.default_rng(SEED)
    rng.shuffle(insts)

    rows, intervals = [], []
    for (trip_id, sdate), g in insts:
        if len(rows) >= N_SAMPLE:
            break
        g = g.sort_values("timestamp").reset_index(drop=True)
        # a vehicle can broadcast its assigned trip_id outside the actual
        # service run (pre-trip, layover) -> keep the densest contiguous run
        gaps = g["timestamp"].diff().dt.total_seconds().fillna(0)
        run = (gaps > 600).cumsum()
        g = g[run == run.value_counts().idxmax()].reset_index(drop=True)
        if len(g) < 30:
            continue
        # unit-proof epoch seconds (parquet stores tz-aware us-resolution)
        t = (g["timestamp"] - pd.Timestamp(0, tz="UTC")) \
            .dt.total_seconds().to_numpy()
        dur = t[-1] - t[0]
        if not (300 <= dur <= 3 * 3600) or np.max(np.diff(t)) > 300:
            continue
        mm = get_matcher_for(trip_id)
        if mm is None:
            continue
        res = mm.match(g["latitude"].to_numpy(), g["longitude"].to_numpy())
        if res.on_route.mean() < C.MIN_ON_ROUTE_FRAC:
            continue
        xc, keep = clean_dist_along(t, res.dist_along_m, res.on_route)
        if keep.sum() < 30 or xc[-1] - xc[0] < C.MIN_FORWARD_M:
            continue
        tt = t[keep]
        if np.max(np.diff(tt)) > 300:
            continue
        key = f"tl_{trip_id}_{sdate}"

        # M1: 5% self-holdout, PCHIP (position-only feed)
        hrng = np.random.default_rng((C.HOLDOUT_SEED, zlib.crc32(key.encode())))
        held = np.zeros(len(tt), dtype=bool)
        held[hrng.choice(len(tt), max(1, int(round(len(tt) * C.HOLDOUT_FRACTION))),
                         replace=False)] = True
        tk, xk = tt[~held], xc[~held]
        inside = held & (tt >= tk[0]) & (tt <= tk[-1])
        if inside.sum() < 1:
            continue
        H = M.pchip(tk, xk)
        mae_x = float(np.mean(np.abs(H.pos(tt[inside]) - xc[inside])))

        # M4: acceleration realism of the full-feed reconstruction
        Hf = M.pchip(tt, xc)
        grid = np.arange(np.ceil(tt[0]), np.floor(tt[-1]) + 0.5, 1.0)
        acc = np.diff(Hf.vel(grid)) / 1.0
        rows.append({
            "trip_key": key, "cadence_s": float(np.median(np.diff(tt))),
            "n_pings": int(keep.sum()), "ho_mae_x": mae_x,
            "n_accel": len(acc),
            "accel_ok_tight": int(((acc >= C.ACCEL_TIGHT[0])
                                   & (acc <= C.ACCEL_TIGHT[1])).sum()),
            "accel_ok_loose": int(((acc >= C.ACCEL_LOOSE[0])
                                   & (acc <= C.ACCEL_LOOSE[1])).sum())})
        intervals.append(np.diff(tt))

    out = pd.DataFrame(rows)
    out.to_csv(C.RESULTS_DIR / out_csv, index=False)
    iv = np.concatenate(intervals)
    print(f"TransLink {route}: {len(out)} sampled trips | interval mean "
          f"{iv.mean():.1f}s median {np.median(iv):.0f}s | "
          f"M1={out.ho_mae_x.mean():.2f} m | "
          f"M4a={100 * out.accel_ok_tight.sum() / out.n_accel.sum():.1f}% "
          f"M4b={100 * out.accel_ok_loose.sum() / out.n_accel.sum():.1f}%")


if __name__ == "__main__":
    import sys
    main(sys.argv[1] if len(sys.argv) > 1 else "R99")
