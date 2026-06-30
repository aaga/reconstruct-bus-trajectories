"""VTRAK along-route speed = d/dt of LOCREG-PCHIP, at bw=15/25/50.

For vehicles 8114, 8089, 1566 we take the same complete trip / GTFS shape as
build_vtrak_smooth.py, map-match the dense VTRAK pings, fit LOCREG-PCHIP at
three bandwidths, and plot the analytic derivative f'(t) in mph. The raw VTRAK
reported speedometer value is overlaid as faint reference dots.

One figure per vehicle: figures/vtrak_speed_<veh>_route<route>.png.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

from build_vtrak_smooth import VEH, OUTDIR, load_r2, load_rocket, pick_trip, best_shape
from bus_trajectories.smooth import locreg_pchip

MPS_TO_MPH = 2.2369363
BANDWIDTHS = [15, 25, 50]
STYLE = {15: dict(color="#2ca02c", ls="-", lw=1.5),
         25: dict(color="#1f77b4", ls="-", lw=1.6),
         50: dict(color="#d62728", ls="--", lw=1.8)}


def main():
    r2, rocket = load_r2(), load_rocket()
    for veh, (route, shapes) in VEH.items():
        g = r2[r2.vehicle_id == veh].copy()
        rk = rocket[rocket.VEH_ID.astype(str) == veh].copy()
        window = (rk.ts_utc.min(), rk.ts_utc.max())
        trip = pick_trip(g, window)
        sid, _, matcher = best_shape(trip, shapes)
        t0, t1 = trip.timestamp.min(), trip.timestamp.max()

        rk_win = rk[(rk.ts_utc >= t0) & (rk.ts_utc <= t1)].copy()
        res = matcher.match(rk_win.LATITUDE.to_numpy(), rk_win.LONGITUDE.to_numpy())
        on = res.on_route
        tt = rk_win.ts_utc.dt.tz_convert("UTC").dt.tz_localize(None).to_numpy()
        t_sec = (tt - tt[0]).astype("timedelta64[ms]").astype(float) / 1000.0
        ts, ds = t_sec[on], res.dist_along_m[on]
        raw_speed = rk_win.SPEED.to_numpy()[on]
        print(f"VEH {veh} route {route} shape {sid}: {on.sum()} on-route VTRAK pings")

        fig, ax = plt.subplots(figsize=(20, 11), dpi=200)
        ax.scatter(ts / 60.0, raw_speed, s=5, c="#999999", alpha=0.35,
                   label="raw VTRAK reported speed", zorder=2)
        tdense = np.linspace(ts.min(), ts.max(), 6000)
        for bw in BANDWIDTHS:
            sm = locreg_pchip(ts, ds, bandwidth=bw, degree=3)
            v_mph = sm.f.derivative()(tdense) * MPS_TO_MPH
            ax.plot(tdense / 60.0, v_mph, label=f"f'(t) LOCREG-PCHIP bw={bw}",
                    zorder=4 + bw, **STYLE[bw])

        ax.axhline(0, color="k", lw=0.6, alpha=0.5)
        ax.set_xlabel("minutes since trip start", fontsize=13)
        ax.set_ylabel("along-route speed (mph)", fontsize=13)
        ax.set_title(
            f"Vehicle {veh} — Route {route} — trip {trip.trip_id.iloc[0]} (shape {sid})\n"
            f"{pd.Timestamp(t0).tz_convert('America/Chicago'):%Y-%m-%d %H:%M}–"
            f"{pd.Timestamp(t1).tz_convert('America/Chicago'):%H:%M} America/Chicago   "
            f"VTRAK speed = d/dt LOCREG-PCHIP (bw=15/25/50)",
            fontsize=14)
        ax.grid(True, which="both", alpha=0.3)
        ax.minorticks_on()
        ax.legend(loc="best", fontsize=12, framealpha=0.9)
        fig.tight_layout()
        out = OUTDIR / f"vtrak_speed_{veh}_route{route}.png"
        fig.savefig(out, dpi=200)
        plt.close(fig)
        print(f"  -> wrote {out}")


if __name__ == "__main__":
    main()
