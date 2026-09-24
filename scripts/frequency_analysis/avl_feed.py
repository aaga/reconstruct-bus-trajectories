"""Project the real low-frequency CTA AVL archive onto each cached trip.

Unlike the R2 BusTime reference (position-only), this feed carries a
``speed`` field — in **ft/s** (unit inferred from distance/time regression;
VTRAK's is mph) — so it can be scored with the velocity-aware methods too. Event-driven cadence,
~16-30 s median; ``avl_event_time`` is naive America/Chicago, same clock
as VTRAK ``dtime``.

    uv run python avl_feed.py     # writes cache/avl_trip_pings.parquet
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd

import config as C
from trip_index import clean_dist_along
from pipeline import epoch_s

AVL_DIR = ("/Users/ashwinagarwal/Library/CloudStorage/"
           "OneDrive-ChicagoTransitAuthority/CTA AVL Archive/avl_archive")


def load_avl_latlon(dates: set[str]) -> pd.DataFrame:
    cache = C.CACHE_DIR / "avl_latlon.parquet"
    if cache.exists():
        return pd.read_parquet(cache)
    frames = []
    for d in sorted(dates):
        f = f"{AVL_DIR}/date={d}.parquet"
        try:
            df = pd.read_parquet(f, columns=["bus_id", "avl_event_time",
                                             "speed", "latitude", "longitude"])
        except FileNotFoundError:
            continue
        df = df[df["bus_id"].isin([int(v) for v in C.VEHICLES])]
        if len(df):
            frames.append(df)
    df = pd.concat(frames, ignore_index=True)
    df["veh_id"] = df["bus_id"].astype(str)
    df = (df.rename(columns={"avl_event_time": "ping_dt"})
            .drop_duplicates(["veh_id", "ping_dt"])
            .sort_values(["veh_id", "ping_dt"]).reset_index(drop=True))
    # AVL archive speeds are in ft/s — inferred from distance/time regression
    # (fitted 0.299 m/s per unit ~ 0.3048; VTRAK independently fits mph).
    df["v_mps"] = pd.to_numeric(df["speed"], errors="coerce") * C.FT_TO_M
    out = df[["veh_id", "ping_dt", "latitude", "longitude", "v_mps"]]
    out.to_parquet(cache)
    return out


def build() -> None:
    from analysis.comparison import matcher_for
    meta = json.loads((C.CACHE_DIR / "trips.json").read_text())
    pings = pd.read_parquet(C.CACHE_DIR / "trip_pings.parquet",
                            columns=["trip_key", "ping_dt"])
    dates = {str(m["trip_key"].split("_")[-1][:8]) for m in meta}
    dates = {f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in dates}
    # trips can cross midnight; include the following day too
    dates |= {str((pd.Timestamp(d) + pd.Timedelta(days=1)).date()) for d in set(dates)}
    avl = load_avl_latlon(dates)

    frames = []
    for m in meta:
        k = m["trip_key"]
        w = pings.loc[pings["trip_key"] == k, "ping_dt"]
        g = avl[(avl["veh_id"] == m["veh_id"])
                & (avl["ping_dt"] >= w.min()) & (avl["ping_dt"] <= w.max())]
        g = g.reset_index(drop=True)
        if len(g) < 5:
            continue
        res = matcher_for(m["shape_id"]).match(
            g["latitude"].to_numpy(), g["longitude"].to_numpy())
        t_s = epoch_s(g["ping_dt"])
        x_clean, keep = clean_dist_along(t_s, res.dist_along_m, res.on_route)
        g = g[keep].reset_index(drop=True)
        g["x_m"] = x_clean
        g["trip_key"] = k
        frames.append(g[["trip_key", "ping_dt", "x_m", "v_mps"]])
    out = pd.concat(frames, ignore_index=True)
    out.to_parquet(C.CACHE_DIR / "avl_trip_pings.parquet")
    cad = out.groupby("trip_key")["ping_dt"].apply(
        lambda s: s.diff().dt.total_seconds().median())
    n_v = out["v_mps"].notna().mean()
    print(f"AVL pings for {out.trip_key.nunique()} trips ({len(out)} pings); "
          f"median cadence {cad.median():.1f}s (IQR {cad.quantile(.25):.1f}-"
          f"{cad.quantile(.75):.1f}); speed present {n_v:.1%}")


if __name__ == "__main__":
    build()
