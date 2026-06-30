"""Smooth the dense raw VTRAK (ROCKET) pings with LOCREG-PCHIP at two bandwidths.

For vehicles 8114, 8089, 1566 we take the SAME complete trip / GTFS shape used
in build_rocket_vs_r2.py, map-match the dense VTRAK pings onto that shape, and
fit LOCREG-PCHIP at bandwidth 25 and 50. One figure per vehicle showing raw
dots + both smoothed curves.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from bus_trajectories.io import load_gtfs_shape_with_dist
from bus_trajectories.mapmatch import get_matcher
from bus_trajectories.smooth import locreg_pchip

ROOT = Path(__file__).resolve().parent.parent
GTFS = ROOT / "cta_gtfs.zip"
ROCKET_CSV = ROOT / "ROCKET_june_8_8am_10am.csv"
R2_HOURS = [
    ROOT / "r2_cache/agency=cta__year=2026__month=06__day=08__hour=13.parquet",
    ROOT / "r2_cache/agency=cta__year=2026__month=06__day=08__hour=14.parquet",
]
OUTDIR = ROOT / "figures"
MAX_PERP_M = 50.0
BANDWIDTHS = [25, 50]

# vehicle_id -> (route, [candidate shapes both directions])
VEH = {
    "8114": ("55", ["67805425", "67805424"]),
    "8089": ("94", ["67814118", "67814119"]),
    "1566": ("X49", ["67807871", "67807873"]),
}


def build_matcher(shape_id: str):
    poly, dist = load_gtfs_shape_with_dist(GTFS, shape_id)
    kw = {"polyline_latlon": poly, "max_perp_m": MAX_PERP_M}
    if dist is not None:
        kw["dist_along_m_per_vertex"] = dist
    return get_matcher("shape_snap", **kw)


def load_r2():
    df = pd.concat([pq.read_table(str(f)).to_pandas() for f in R2_HOURS],
                   ignore_index=True)
    df = df[df.vehicle_id.isin(VEH)].copy()
    return df.drop_duplicates(["vehicle_id", "timestamp"]).sort_values(
        ["vehicle_id", "timestamp"]).reset_index(drop=True)


def load_rocket():
    df = pd.read_csv(ROCKET_CSV)
    df = df[df.VEH_ID.astype(str).isin(VEH)].copy()
    t = pd.to_datetime(df.AVL_EVENT_TIME)
    df["ts_utc"] = t.dt.tz_localize("America/Chicago", ambiguous="NaT",
                                    nonexistent="NaT").dt.tz_convert("UTC")
    return df.dropna(subset=["ts_utc"]).sort_values(["VEH_ID", "ts_utc"])


def pick_trip(g, window):
    lo, hi = window
    best = None
    for _, tg in g.groupby("trip_id"):
        t0, t1 = tg.timestamp.min(), tg.timestamp.max()
        if t0 < lo or t1 > hi:
            continue
        if best is None or len(tg) > len(best):
            best = tg
    return best.sort_values("timestamp").reset_index(drop=True)


def best_shape(trip, shape_ids):
    lats, lons = trip.latitude.to_numpy(), trip.longitude.to_numpy()
    best = None
    for sid in shape_ids:
        m = build_matcher(sid)
        res = m.match(lats, lons)
        on = res.on_route
        score = np.median(res.perp_dist_m[on]) if on.sum() else np.inf
        if best is None or (on.mean() > 0.5 and score < best[1]):
            best = (sid, score, m)
    return best


def main():
    r2, rocket = load_r2(), load_rocket()
    for veh, (route, shapes) in VEH.items():
        g = r2[r2.vehicle_id == veh].copy()
        rk = rocket[rocket.VEH_ID.astype(str) == veh].copy()
        window = (rk.ts_utc.min(), rk.ts_utc.max())
        trip = pick_trip(g, window)
        sid, _, matcher = best_shape(trip, shapes)
        t0 = trip.timestamp.min()
        t1 = trip.timestamp.max()

        # dense VTRAK in the trip window, map-matched onto the shape
        rk_win = rk[(rk.ts_utc >= t0) & (rk.ts_utc <= t1)].copy()
        res = matcher.match(rk_win.LATITUDE.to_numpy(), rk_win.LONGITUDE.to_numpy())
        on = res.on_route
        tt = rk_win.ts_utc.dt.tz_convert("UTC").dt.tz_localize(None).to_numpy()
        t_sec = (tt - tt[0]).astype("timedelta64[ms]").astype(float) / 1000.0
        t_min = t_sec / 60.0
        d_km = res.dist_along_m / 1000.0

        ts, ds = t_sec[on], res.dist_along_m[on]
        print(f"VEH {veh} route {route} shape {sid}: {on.sum()} on-route VTRAK pings "
              f"({(~on).sum()} off-route), {t_min[on].max():.1f} min")

        fig, ax = plt.subplots(figsize=(20, 11), dpi=200)
        style = {25: dict(color="#1f77b4", ls="-", lw=1.6),
                 50: dict(color="#d62728", ls="--", lw=1.6)}
        for bw in BANDWIDTHS:
            sm = locreg_pchip(ts, ds, bandwidth=bw, degree=3)
            tdense = np.linspace(ts.min(), ts.max(), 4000)
            ax.plot(tdense / 60.0, sm.f(tdense) / 1000.0,
                    label=f"LOCREG-PCHIP bw={bw}", zorder=4 + bw, **style[bw])
        ax.scatter(t_min[on], d_km, s=5, c="#444444", alpha=0.45,
                   label=f"raw VTRAK pings ({on.sum()})", zorder=10)

        ax.set_xlabel("minutes since trip start", fontsize=13)
        ax.set_ylabel("distance along route shape (km)", fontsize=13)
        ax.set_title(
            f"Vehicle {veh} — Route {route} — trip {trip.trip_id.iloc[0]} (shape {sid})\n"
            f"{pd.Timestamp(t0).tz_convert('America/Chicago'):%Y-%m-%d %H:%M}–"
            f"{pd.Timestamp(t1).tz_convert('America/Chicago'):%H:%M} America/Chicago   "
            f"raw VTRAK smoothed with LOCREG-PCHIP (bw=25 vs 50)",
            fontsize=14)
        ax.grid(True, which="both", alpha=0.3)
        ax.minorticks_on()
        ax.legend(loc="best", fontsize=12, framealpha=0.9)
        fig.tight_layout()
        out = OUTDIR / f"vtrak_smooth_{veh}_route{route}.png"
        fig.savefig(out, dpi=200)
        plt.close(fig)
        print(f"  -> wrote {out}")


if __name__ == "__main__":
    main()
