"""Project the real R2 BusTime pings onto each cached trip (reference feed).

The R2 archive (~15 s full-fleet feed, position only — the speed field is
empty for CTA) is the feed agencies actually have today. Scoring a PCHIP
reconstruction built from the *real* R2 pings and placing it at its measured
cadence on every metric plot validates the downsampling methodology: if
"VTRAK thinned to ~15 s" behaves like "the real 15 s feed", the ladder is a
faithful stand-in.

    uv run python r2_feed.py     # writes cache/r2_trip_pings.parquet
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd

import config as C
from trip_index import clean_dist_along


def load_r2_latlon() -> pd.DataFrame:
    cache = C.CACHE_DIR / "r2_latlon.parquet"
    if cache.exists():
        return pd.read_parquet(cache)
    frames = []
    for f in sorted(C.R2_DIR.glob(f"agency={C.R2_AGENCY}__year=*.parquet")):
        df = pd.read_parquet(
            f, columns=["vehicle_id", "timestamp", "latitude", "longitude"])
        df = df[df["vehicle_id"].astype(str).isin(C.VEHICLES)]
        if len(df):
            frames.append(df)
    df = pd.concat(frames, ignore_index=True)
    df["vehicle_id"] = df["vehicle_id"].astype(str)
    ts = pd.to_datetime(df["timestamp"], utc=True)
    df["ping_dt"] = ts.dt.tz_convert(C.LOCAL_TZ).dt.tz_localize(None)
    df = (df.drop_duplicates(["vehicle_id", "ping_dt"])
            .sort_values(["vehicle_id", "ping_dt"]).reset_index(drop=True))
    out = df[["vehicle_id", "ping_dt", "latitude", "longitude"]]
    out.to_parquet(cache)
    return out


def build() -> None:
    from analysis.comparison import matcher_for
    meta = json.loads((C.CACHE_DIR / "trips.json").read_text())
    pings = pd.read_parquet(C.CACHE_DIR / "trip_pings.parquet",
                            columns=["trip_key", "ping_dt"])
    r2 = load_r2_latlon()
    frames = []
    for m in meta:
        k = m["trip_key"]
        w = pings.loc[pings["trip_key"] == k, "ping_dt"]
        g = r2[(r2["vehicle_id"] == m["veh_id"])
               & (r2["ping_dt"] >= w.min()) & (r2["ping_dt"] <= w.max())]
        g = g.reset_index(drop=True)
        if len(g) < 5:
            continue
        res = matcher_for(m["shape_id"]).match(
            g["latitude"].to_numpy(), g["longitude"].to_numpy())
        from pipeline import epoch_s
        t_s = epoch_s(g["ping_dt"])
        x_clean, keep = clean_dist_along(t_s, res.dist_along_m, res.on_route)
        g = g[keep].reset_index(drop=True)
        g["x_m"] = x_clean
        g["trip_key"] = k
        frames.append(g[["trip_key", "ping_dt", "x_m"]])
    out = pd.concat(frames, ignore_index=True)
    out.to_parquet(C.CACHE_DIR / "r2_trip_pings.parquet")
    cad = out.groupby("trip_key")["ping_dt"].apply(
        lambda s: s.diff().dt.total_seconds().median())
    print(f"R2 pings for {out.trip_key.nunique()} trips "
          f"({len(out)} pings); median cadence {cad.median():.1f}s "
          f"(IQR {cad.quantile(.25):.1f}-{cad.quantile(.75):.1f})")


if __name__ == "__main__":
    build()
