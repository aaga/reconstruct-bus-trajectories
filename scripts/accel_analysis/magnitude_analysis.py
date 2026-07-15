"""Sustained horizontal-acceleration magnitude vs. the GPS story.

Per trip, one figure with two aligned panels:

  top    — |a_xy| from the phone accelerometer (low-passed at 0.4 Hz — the
           "sustained >=1-2 s" criterion) vs. |a| inferred from the GPS
           reconstruction (LOCREG-PCHIP f''), with the signed GPS accel shown
           faintly for accel-vs-brake context.
  bottom — the GPS speed story: raw device speed dots + reconstructed v(t).

Phone-handling periods (sustained rotation > 60 deg/s) are shaded; they are
excluded from the agreement stats. Stats per trip: Pearson r between the two
accel series on the 1 Hz GPS grid, plus coverage numbers →
outputs/accel_analysis/results/magnitude_stats.csv.

    PYTHONPATH=src uv run python scripts/accel_analysis/magnitude_analysis.py
        [--keys K1 K2 ...]   default: the 9 dashboard trips, else whatever
                             local exports contain
        [--extra-dir PATH]   additional export folder(s) to search
"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import common as CM  # noqa: E402

C_PHONE = "#1f77b4"   # phone accel magnitude
C_GPS = "#ff7f0e"     # GPS-inferred accel magnitude
C_SIGNED = "#bbbbbb"  # signed GPS accel (context)
C_SPEED = "#2ca02c"   # reconstructed speed
C_MASK = "#d62728"    # handling shading


def analyze_trip(key: str, extra_dirs: list[Path]) -> dict | None:
    try:
        trip = CM.load_motion(key, extra_dirs)
    except FileNotFoundError as e:
        print(f"  !! {e}")
        return None
    try:
        gps = CM.load_gps(trip)
    except ValueError as e:
        print(f"  !! {e} — producing accel-only figure")
        gps = None

    # ---- common 1 Hz grid (the GPS reconstruction clock)
    r = np.nan
    valid = np.zeros(0, bool)
    a_phone = a_gps = tg = np.zeros(0)
    gg = None
    if gps is not None:
        gg = gps.dropna(subset=["a_recon_ms2"])
        tg = gg.t.to_numpy()
        ok = ~trip.handling & np.isfinite(trip.a_horiz)
        a_phone = np.interp(tg, trip.t[ok], trip.a_horiz[ok])
        handling_g = np.interp(tg, trip.t, trip.handling.astype(float)) > 0.25
        # only compare where motion data actually exists nearby
        has_motion = np.zeros(len(tg), bool)
        for i0, i1 in trip.segments:
            has_motion |= (tg >= trip.t[i0]) & (tg <= trip.t[i1 - 1])
        valid = has_motion & ~handling_g
        a_gps = np.abs(gg.a_recon_ms2.to_numpy())
        if valid.sum() > 30:
            r = float(np.corrcoef(a_phone[valid], a_gps[valid])[0, 1])

    # ---------------------------------------------------------------- figure
    tmin = trip.t / 60.0
    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(16, 7), dpi=180, sharex=True,
        gridspec_kw={"height_ratios": [3, 2], "hspace": 0.08})

    ax1.plot(tmin, trip.a_horiz, color=C_PHONE, lw=1.0,
             label="phone |a_xy| (sustained, 0.4 Hz LP)")
    if gg is not None:
        ax1.plot(tg / 60, a_gps, color=C_GPS, lw=1.4, alpha=0.9,
                 label="|a| from GPS reconstruction (LOCREG-PCHIP f'')")
        ax1.plot(tg / 60, gg.a_recon_ms2.to_numpy(), color=C_SIGNED, lw=0.8,
                 zorder=1, label="signed GPS accel (context)")
    ax1.axhline(0, color="#999999", lw=0.5)
    ax1.set_ylabel("acceleration (m/s²)")
    ax1.set_ylim(-3, 3)
    ax1.legend(loc="upper right", fontsize=8, frameon=False, ncols=3)

    if gps is not None:
        ax2.plot(gps.t / 60, gps.speed_mps * CM.MPS_TO_MPH, ".", ms=2,
                 color="#888888", label="device GPS speed")
        ax2.plot(gps.t / 60, gps.v_recon_mps * CM.MPS_TO_MPH, color=C_SPEED,
                 lw=1.4, label="reconstructed v(t)")
        ax2.legend(loc="upper right", fontsize=8, frameon=False, ncols=2)
    else:
        ax2.text(0.5, 0.5, "no usable GPS for this trip", transform=ax2.transAxes,
                 ha="center", color="#888888")
    ax2.set_ylabel("speed (mph)")
    ax2.set_xlabel("minutes since first motion sample")

    for ax in (ax1, ax2):
        ax.grid(True, alpha=0.25, lw=0.6)
        ax.spines[["top", "right"]].set_visible(False)
        started = None
        h = trip.handling
        for i in range(len(h)):
            if h[i] and started is None:
                started = trip.t[i]
            elif not h[i] and started is not None:
                ax.axvspan(started / 60, trip.t[i] / 60, color=C_MASK, alpha=0.10, lw=0)
                started = None
        if started is not None:
            ax.axvspan(started / 60, trip.t[-1] / 60, color=C_MASK, alpha=0.10, lw=0)

    meta = trip.meta
    label = (f"{key} — rt {meta.get('route_id', '?')} bus {meta.get('bus_id', '?')} "
             f"→ {meta.get('destination', '')}")
    ax1.set_title(
        f"{label}\n"
        f"gravity={trip.gravity_mode} · {trip.hz:.0f} Hz · "
        f"handling-masked {100 * trip.handling.mean():.1f}% (red shading) · "
        f"corr(|a_phone|, |a_gps|) = {r:.2f}",
        fontsize=11, loc="left")

    CM.FIG_DIR.mkdir(parents=True, exist_ok=True)
    out = CM.FIG_DIR / f"accel_{key}.png"
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    print(f"  -> {out.name}  (r={r:.2f})")

    return {
        "key": key, "route_id": meta.get("route_id"), "bus_id": meta.get("bus_id"),
        "gravity_mode": trip.gravity_mode, "hz": round(trip.hz, 1),
        "duration_min": round(trip.t[-1] / 60, 1),
        "handling_frac": round(float(trip.handling.mean()), 4),
        "n_grid": int(valid.sum()),
        "corr_accel": round(r, 3),
        "a_phone_p95": round(float(np.percentile(a_phone[valid], 95)), 3) if valid.sum() else np.nan,
        "a_gps_p95": round(float(np.percentile(a_gps[valid], 95)), 3) if valid.sum() else np.nan,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--keys", nargs="*", default=None)
    ap.add_argument("--extra-dir", nargs="*", default=[], type=Path)
    args = ap.parse_args()

    keys = args.keys
    if not keys:
        # dashboard trips that are locally available; else all local trips
        keys = [k for k in CM.DASHBOARD_KEYS if CM.locate_trip(k, args.extra_dir)]
        if not keys:
            keys = sorted({p.name for d in CM.find_export_dirs()
                           for p in d.iterdir()
                           if (p / "motion.csv").exists()})
            print(f"no dashboard trips found locally — using {len(keys)} local trip(s)")

    rows = []
    for key in keys:
        res = analyze_trip(key, args.extra_dir)
        if res:
            rows.append(res)
    if rows:
        CM.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        df = pd.DataFrame(rows)
        df.to_csv(CM.RESULTS_DIR / "magnitude_stats.csv", index=False)
        print("\n" + df.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
