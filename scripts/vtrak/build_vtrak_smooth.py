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

from core.smooth import locreg_pchip
from dataio.vtrak import (
    best_shape as _best_shape,
    build_shape_matcher,
    load_r2_hours,
    load_rocket_csv,
    pick_trip_in_window,
)

ROOT = Path(__file__).resolve().parents[2]
GTFS = ROOT / "data" / "gtfs" / "cta_gtfs.zip"
ROCKET_CSV = ROOT / "data" / "ROCKET_june_8_8am_10am.csv"
ARCHIVE_HOURS = [
    ROOT / "caches/realtime_archive/agency=cta__year=2026__month=06__day=08__hour=13.parquet",
    ROOT / "caches/realtime_archive/agency=cta__year=2026__month=06__day=08__hour=14.parquet",
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


# Thin wrappers binding the shared dataio.vtrak helpers to this
# script's constants. Kept as module-level names because build_vtrak_speed,
# build_pchip_vs_mqsi and build_smoothing_dashboard import them from here.
def build_matcher(shape_id: str):
    return build_shape_matcher(GTFS, shape_id, MAX_PERP_M)


def load_r2():
    return load_r2_hours(ARCHIVE_HOURS, veh_ids=VEH)


def load_rocket():
    return load_rocket_csv(ROCKET_CSV, veh_ids=VEH)


def pick_trip(g, window):
    return pick_trip_in_window(g, window)


def best_shape(trip, shape_ids):
    """(shape_id, median_perp, matcher) — drops the coverage field the shared
    helper returns, matching the 3-tuple the importing scripts expect."""
    sid, score, _cov, matcher = _best_shape(trip, shape_ids, GTFS, MAX_PERP_M)
    return sid, score, matcher


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
