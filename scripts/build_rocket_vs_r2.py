"""Overlay raw VTRAK (ROCKET) pings on the smoothed R2 trajectory.

For four CTA vehicles (8114, 8099, 8089, 1566) we:

  1. Pull their pings from the R2 BusTime archive for 2026-06-08 08:00-10:00
     Chicago (= 13:00-15:00 UTC), pick one complete trip per vehicle.
  2. Auto-select the best-fitting GTFS shape for that trip's route by minimum
     median perpendicular snap distance.
  3. Map-match the R2 heartbeats onto that shape and smooth with LOCREG-PCHIP
     (bandwidth 5) to recover f(t) = distance-into-trip.
  4. Map-match the dense raw VTRAK pings (ROCKET CSV) for the same vehicle,
     restricted to the trip's time window, onto the same shape.
  5. Render a high-resolution time-space diagram: smoothed R2 curve + R2
     heartbeats + raw VTRAK pings.

One figure per vehicle in figures/rocket_vs_r2_<veh>_route<route>.png.
"""

from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from bus_trajectories.smooth import locreg_pchip
from bus_trajectories.vtrak import (
    best_shape as _best_shape,
    build_shape_matcher,
    load_r2_hours,
    load_rocket_csv,
    pick_trip_in_window,
)

ROOT = Path(__file__).resolve().parent.parent
GTFS = ROOT / "data" / "gtfs" / "cta_gtfs.zip"
ROCKET_CSV = ROOT / "data" / "ROCKET_june_8_8am_10am.csv"
R2_HOURS = [
    ROOT / "caches/r2_cache/agency=cta__year=2026__month=06__day=08__hour=13.parquet",
    ROOT / "caches/r2_cache/agency=cta__year=2026__month=06__day=08__hour=14.parquet",
]
OUTDIR = ROOT / "figures"

BANDWIDTH = 5
MAX_PERP_M = 50.0

# vehicle_id -> candidate GTFS shapes (both directions of its route)
VEH_SHAPES = {
    "8114": ["67805425", "67805424"],            # route 55  Garfield
    "8099": ["67807111", "67807120"],            # route 62  Archer
    "8089": ["67814118", "67814119"],            # route 94  California
    "1566": ["67807871", "67807873"],            # route X49 Western Express
}
VEH_ROUTE = {"8114": "55", "8099": "62", "8089": "94", "1566": "X49"}


# Thin wrappers binding the shared bus_trajectories.vtrak helpers to this
# script's constants.
def load_r2() -> pd.DataFrame:
    return load_r2_hours(R2_HOURS, veh_ids=VEH_SHAPES)


def load_rocket() -> pd.DataFrame:
    return load_rocket_csv(ROCKET_CSV, veh_ids=VEH_SHAPES)


def build_matcher(shape_id: str):
    return build_shape_matcher(GTFS, shape_id, MAX_PERP_M)


def pick_trip(g: pd.DataFrame, window) -> pd.DataFrame:
    return pick_trip_in_window(g, window)


def best_shape(trip: pd.DataFrame, shape_ids: list[str]):
    """(shape_id, median_perp, coverage, matcher) for the best-fitting shape."""
    return _best_shape(trip, shape_ids, GTFS, MAX_PERP_M)


def main():
    r2 = load_r2()
    rocket = load_rocket()

    for veh, shape_ids in VEH_SHAPES.items():
        route = VEH_ROUTE[veh]
        g = r2[r2.vehicle_id == veh].copy()
        rk = rocket[rocket.VEH_ID.astype(str) == veh].copy()
        # ROCKET coverage window for this vehicle (UTC), used to require a
        # complete trip we can fully compare against.
        window = (rk.ts_utc.min(), rk.ts_utc.max())

        trip = pick_trip(g, window)
        sid, med_perp, cov, matcher = best_shape(trip, shape_ids)
        t0 = trip.timestamp.min()
        t1 = trip.timestamp.max()
        print(f"VEH {veh} route {route}: trip {trip.trip_id.iloc[0]} "
              f"shape {sid} medperp {med_perp:.1f}m cov {cov:.2f} "
              f"{t0}..{t1} ({len(trip)} R2 pings)")

        # --- R2: map-match + smooth ---
        res = matcher.match(trip.latitude.to_numpy(), trip.longitude.to_numpy())
        on = res.on_route
        tt = trip.timestamp.dt.tz_convert("UTC").dt.tz_localize(None).to_numpy()
        t_rel = (tt - tt[0]).astype("timedelta64[s]").astype(float) / 60.0  # minutes
        d_r2 = res.dist_along_m / 1000.0  # km
        sm = locreg_pchip((tt[on] - tt[0]).astype("timedelta64[ms]").astype(float) / 1000.0,
                          res.dist_along_m[on], bandwidth=BANDWIDTH, degree=3)
        # dense smoothed curve
        ts_dense = np.linspace(sm.t.min(), sm.t.max(), 2000)
        d_dense = sm.f(ts_dense) / 1000.0
        tmin_dense = ts_dense / 60.0

        # --- ROCKET: same shape, restricted to trip window ---
        rk_win = rk[(rk.ts_utc >= t0) & (rk.ts_utc <= t1)].copy()
        rres = matcher.match(rk_win.LATITUDE.to_numpy(), rk_win.LONGITUDE.to_numpy())
        ron = rres.on_route
        rk_tt = rk_win.ts_utc.dt.tz_convert("UTC").dt.tz_localize(None).to_numpy()
        rk_t = (rk_tt - tt[0]).astype("timedelta64[ms]").astype(float) / 60000.0
        rk_d = rres.dist_along_m / 1000.0

        # --- plot ---
        fig, ax = plt.subplots(figsize=(20, 11), dpi=200)
        ax.scatter(rk_t[ron], rk_d[ron], s=4, c="#1f77b4", alpha=0.55,
                   label=f"raw VTRAK pings (ROCKET, {ron.sum()})", zorder=2)
        ax.plot(tmin_dense, d_dense, "-", color="#d62728", lw=2.0,
                label="smoothed R2 trajectory (LOCREG-PCHIP bw=5)", zorder=4)
        ax.scatter(t_rel[on], d_r2[on], s=42, facecolors="none",
                   edgecolors="#000000", linewidths=1.2,
                   label=f"R2 BusTime heartbeats ({on.sum()})", zorder=5)
        off = ~ron
        if off.sum():
            ax.scatter(rk_t[off], rk_d[off], s=3, c="#bbbbbb", alpha=0.3,
                       label=f"VTRAK off-route (>{MAX_PERP_M:.0f}m, {off.sum()})", zorder=1)

        ax.set_xlabel("minutes since trip start", fontsize=13)
        ax.set_ylabel("distance along route shape (km)", fontsize=13)
        ax.set_title(
            f"Vehicle {veh} — Route {route} — trip {trip.trip_id.iloc[0]}  "
            f"(shape {sid})\n"
            f"{pd.Timestamp(t0).tz_convert('America/Chicago'):%Y-%m-%d %H:%M} – "
            f"{pd.Timestamp(t1).tz_convert('America/Chicago'):%H:%M} America/Chicago   "
            f"raw VTRAK on smoothed R2 BusTime",
            fontsize=14)
        ax.grid(True, which="both", alpha=0.3)
        ax.minorticks_on()
        ax.legend(loc="best", fontsize=12, framealpha=0.9)
        fig.tight_layout()
        out = OUTDIR / f"rocket_vs_r2_{veh}_route{route}.png"
        fig.savefig(out, dpi=200)
        plt.close(fig)
        print(f"  -> wrote {out}")


if __name__ == "__main__":
    main()
