"""Speed & acceleration: LOCREG-PCHIP vs LOCREG-MQSI at bw=10.

For vehicles 8114, 8089, 1566 (same trip/shape as build_vtrak_smooth.py) smooth
the deduped VTRAK pings two ways and plot the analytic derivatives:
  * speed  = f'(t)  in mph
  * accel  = f''(t) in mph/s
PCHIP is monotone C^1 (its acceleration jumps at every knot); MQSI is monotone
C^2 (continuous acceleration). No raw ping speed shown.

Outputs (6 figures):
  figures/speed_pchip_vs_mqsi_<veh>_route<route>.png
  figures/accel_pchip_vs_mqsi_<veh>_route<route>.png
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

from build_vtrak_smooth import VEH, OUTDIR, load_r2, load_rocket, pick_trip, best_shape
from bus_trajectories.smooth import locreg_pchip, locreg_mqsi

MPS_TO_MPH = 2.2369363
BW = 25
PCHIP_C = "#1f77b4"
MQSI_C = "#d62728"


def trip_series(veh, route, shapes, r2, rocket):
    g = r2[r2.vehicle_id == veh].copy()
    rk = rocket[rocket.VEH_ID.astype(str) == veh].copy()
    window = (rk.ts_utc.min(), rk.ts_utc.max())
    trip = pick_trip(g, window)
    sid, _, matcher = best_shape(trip, shapes)
    t0, t1 = trip.timestamp.min(), trip.timestamp.max()
    w = rk[(rk.ts_utc >= t0) & (rk.ts_utc <= t1)].copy()
    # drop the 1 Hz VTRAK doublet (consecutive identical fixes)
    keep = (w.LATITUDE != w.LATITUDE.shift()) | (w.LONGITUDE != w.LONGITUDE.shift())
    w = w[keep]
    res = matcher.match(w.LATITUDE.to_numpy(), w.LONGITUDE.to_numpy())
    on = res.on_route
    tt = w.ts_utc.dt.tz_convert("UTC").dt.tz_localize(None).to_numpy()
    t_sec = (tt - tt[0]).astype("timedelta64[ms]").astype(float) / 1000.0
    label = (f"{pd.Timestamp(t0).tz_convert('America/Chicago'):%Y-%m-%d %H:%M}–"
             f"{pd.Timestamp(t1).tz_convert('America/Chicago'):%H:%M} America/Chicago")
    return (t_sec[on], res.dist_along_m[on], trip.trip_id.iloc[0], sid, label)


def main():
    r2, rocket = load_r2(), load_rocket()
    for veh, (route, shapes) in VEH.items():
        ts, ds, trip_id, sid, label = trip_series(veh, route, shapes, r2, rocket)
        pch = locreg_pchip(ts, ds, bandwidth=BW)
        mq = locreg_mqsi(ts, ds, bandwidth=BW)
        tdense = np.linspace(ts.min(), ts.max(), 8000)
        tmin = tdense / 60.0
        head = (f"Vehicle {veh} — Route {route} — trip {trip_id} (shape {sid})\n{label}")

        # --- speed ---
        fig, ax = plt.subplots(figsize=(20, 11), dpi=200)
        ax.plot(tmin, pch.f.derivative(1)(tdense) * MPS_TO_MPH, color=PCHIP_C,
                lw=1.5, label="LOCREG-PCHIP  bw=25  (C¹)")
        ax.plot(tmin, mq.f.derivative(1)(tdense) * MPS_TO_MPH, color=MQSI_C,
                lw=1.5, label="LOCREG-MQSI  bw=25  (C²)")
        ax.axhline(0, color="k", lw=0.6, alpha=0.5)
        ax.set_xlabel("minutes since trip start", fontsize=13)
        ax.set_ylabel("along-route speed (mph)", fontsize=13)
        ax.set_title(head + "   speed = f'(t)", fontsize=14)
        ax.grid(True, which="both", alpha=0.3)
        ax.minorticks_on()
        ax.legend(loc="best", fontsize=12, framealpha=0.9)
        fig.tight_layout()
        out = OUTDIR / f"speed_pchip_vs_mqsi_bw{BW}_{veh}_route{route}.png"
        fig.savefig(out, dpi=200)
        plt.close(fig)
        print(f"  -> wrote {out}")

        # --- acceleration ---
        fig, ax = plt.subplots(figsize=(20, 11), dpi=200)
        ax.plot(tmin, pch.f.derivative(2)(tdense) * MPS_TO_MPH, color=PCHIP_C,
                lw=1.0, alpha=0.7, label="LOCREG-PCHIP  bw=25  (discontinuous accel)")
        ax.plot(tmin, mq.f.derivative(2)(tdense) * MPS_TO_MPH, color=MQSI_C,
                lw=1.0, alpha=0.7, label="LOCREG-MQSI  bw=25  (continuous accel)")
        ax.axhline(0, color="k", lw=0.6, alpha=0.5)
        ax.set_xlabel("minutes since trip start", fontsize=13)
        ax.set_ylabel("along-route acceleration (mph/s)", fontsize=13)
        ax.set_title(head + "   acceleration = f''(t)", fontsize=14)
        ax.grid(True, which="both", alpha=0.3)
        ax.minorticks_on()
        ax.legend(loc="best", fontsize=12, framealpha=0.9)
        fig.tight_layout()
        out = OUTDIR / f"accel_pchip_vs_mqsi_bw{BW}_{veh}_route{route}.png"
        fig.savefig(out, dpi=200)
        plt.close(fig)
        print(f"  -> wrote {out}")


if __name__ == "__main__":
    main()
