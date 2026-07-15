"""Exploratory: pedal events (accelerator/brake) vs steering events (turns,
lane changes) from phone motion data on a bus.

Literature basis
----------------
- Nericell (Mohan et al., SenSys 2008) — virtual reorientation: recover the
  vehicle frame from an arbitrarily-oriented phone.
- Johnson & Trivedi (ITSC 2011, MIROAD) — maneuver signatures in the
  reoriented accel/gyro channels.
- V-Sense (Chen et al., MobiSys 2015) — steering shows up as *bump-shaped
  yaw-rate* excursions; the net heading change classifies the maneuver
  (turn ≈ ±90°, lane change ≈ 0° with an S-shaped yaw profile, U-turn ≈ 180°).

Adaptation to this dataset (linear accel WITHOUT gravity + rotation rate,
both in the rotating phone frame, ~60 Hz; GPS speed/bearing at 1 Hz):

1. **Vertical axis from the gyroscope, not gravity.** A road vehicle
   rotates almost exclusively about the vertical (yaw) axis — pitch/roll are
   small and transient. So over any window where the phone is rigid w.r.t.
   the bus, the dominant principal axis (PCA) of the rotation-rate vectors
   IS the vehicle's vertical, up to sign. We re-estimate it per stable
   window (between handling events) to track phone re-orientations.
2. **Sign + sanity from GPS.** Integrated yaw rate about that axis must
   track the GPS bearing change; low correlation means the phone was not
   rigid and the window is untrustworthy.

**BOTH gyro routes FAILED on this data — a documented negative result**
(see the NEGATIVE RESULT marker below): on a bus the phone rides on a
person, and body sway (~5-7 deg/s at all times) swamps and rotationally
decouples the vehicle yaw. The mounted-phone assumption behind
V-Sense/Nericell does not transfer to transit riders. So instead:

3. **Vehicle yaw from GPS path curvature**: ω = d(bearing)/dt of the
   smoothed GPS track — drift-free, orientation-free, valid while moving
   (>3 m/s), which is exactly when steering accel exists.
4. **Kinematic split of the horizontal acceleration.** Centripetal
   (steering) acceleration is a_lat = v · ω with both factors from GPS — no
   accelerometer axes needed. The steering-explained share of the sustained
   horizontal magnitude |a_xy| (same filtering as magnitude_analysis)
   classifies each sustained-acceleration episode:
       steering  — |a_lat| explains most of |a_xy| (turn vs curve/lane by
                   net heading change, the V-Sense-style shape check)
       pedal     — |a_xy| large while |a_lat| small; sign of GPS dv/dt says
                   accelerator vs brake
       mixed     — both (braking into a turn, accelerating out)
       low-speed — below the bearing-validity floor (stop pull-in/out)

Outputs: per-trip classified-episode timeline figure + episode table CSV.

    PYTHONPATH=src uv run python scripts/accel_analysis/steering_exploration.py [--keys ...]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import common as CM  # noqa: E402

YAW_LP_HZ = 0.4            # sustained-rotation band, matches accel filtering
RIGID_MIN_R = 0.7          # min corr(∫ω_yaw, GPS bearing change) to trust a window
EPISODE_MIN_A = 0.35       # m/s² — sustained |a_xy| to open an episode
EPISODE_MIN_S = 1.5        # s    — the "at least 1-2 full seconds" rule
STEER_FRAC = 0.6           # |a_lat|/|a_xy| above this => steering-dominated
PEDAL_FRAC = 0.35          # below this => pedal-dominated
TURN_DEG = 45.0            # net heading change above this => turn
CURVE_DPS = 1.5            # sustained yaw to count as steering at all


def unwrap_bearing_deg(b: np.ndarray) -> np.ndarray:
    return np.degrees(np.unwrap(np.radians(b)))


def gps_bearing(gps: pd.DataFrame) -> np.ndarray:
    """Track bearing (deg) from consecutive GPS fixes."""
    la = np.radians(gps.lat.to_numpy())
    lo = np.radians(gps.lon.to_numpy())
    dlon = np.diff(lo)
    y = np.sin(dlon) * np.cos(la[1:])
    x = (np.cos(la[:-1]) * np.sin(la[1:])
         - np.sin(la[:-1]) * np.cos(la[1:]) * np.cos(dlon))
    b = np.degrees(np.arctan2(y, x))
    return np.r_[b[0], b]


# ------------------------------------------------------- vertical axis / yaw

MOVING_MPS = 3.0           # bearing is only meaningful when moving

# ---------------------------------------------------------------------------
# NEGATIVE RESULT (kept for the record): two gyro-based yaw recoveries were
# tried first — (a) PCA of rotation-rate vectors for the vertical axis with
# integrated-yaw-vs-GPS-bearing validation, (b) direct least-squares
# regression of the 3 gyro channels onto the GPS bearing rate, per window,
# even restricted to clear-turn moments and scanned over ±3 s clock lag.
# Both failed the rigidity gate on essentially every window (r ≈ 0.15, and
# median |rotation| is ~5-7 deg/s whether or not the bus is turning). On a
# bus the phone rides on a *person*, and body sway swamps + decouples the
# vehicle yaw — the mounted-phone assumption of V-Sense/Nericell does not
# transfer. Vehicle yaw therefore comes from the GPS track itself below.
# ---------------------------------------------------------------------------


def gps_bearing_rate(gps: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(t, ω_gps deg/s, moving mask) — smoothed d(bearing)/dt on the GPS clock."""
    tg = gps.t.to_numpy()
    b = unwrap_bearing_deg(gps_bearing(gps))
    # ~5 s centered rolling mean knocks down fix-to-fix bearing jitter
    bs = pd.Series(b).rolling(5, center=True, min_periods=1).mean().to_numpy()
    w = np.gradient(bs, tg)
    moving = gps.v_recon_mps.fillna(0).to_numpy() > MOVING_MPS
    return tg, w, moving


def estimate_yaw(trip: CM.MotionTrip, gps: pd.DataFrame) -> tuple[np.ndarray, list[dict]]:
    """Vehicle yaw rate (deg/s) on the motion clock, from GPS path curvature.

    ω = d(bearing)/dt of the smoothed GPS track — drift-free and phone-
    orientation-free. Valid only while moving (> 3 m/s): bearing is undefined
    when stopped, so those samples are NaN. That is the honest domain — a bus
    generates steering acceleration only while moving."""
    tg, w_gps, moving_g = gps_bearing_rate(gps)
    yaw = np.interp(trip.t, tg, w_gps)
    mv = np.interp(trip.t, tg, moving_g.astype(float)) > 0.5
    yaw[~mv] = np.nan
    return yaw, []


# ------------------------------------------------------------- episodes

def classify_episodes(trip: CM.MotionTrip, gps: pd.DataFrame,
                      yaw: np.ndarray) -> pd.DataFrame:
    """Sustained |a_xy| episodes -> steering / pedal / mixed."""
    t = trip.t
    v = np.interp(t, gps.t, gps.v_recon_mps.fillna(0.0))
    dvdt = np.interp(t, gps.t, gps.a_recon_ms2.fillna(0.0))
    a_lat = np.abs(v * np.radians(yaw))          # v·ω, m/s²
    a = trip.a_horiz

    on = (a > EPISODE_MIN_A) & ~trip.handling & np.isfinite(a)
    runs = []
    i = 0
    while i < len(on):
        if not on[i]:
            i += 1
            continue
        j = i
        while j < len(on) and on[j]:
            j += 1
        if t[j - 1] - t[i] >= EPISODE_MIN_S:
            runs.append((i, j))
        i = j

    rows = []
    for i0, i1 in runs:
        sl = slice(i0, i1)
        a_mean = float(np.nanmean(a[sl]))
        lat_mean = float(np.nanmean(a_lat[sl])) if np.isfinite(a_lat[sl]).any() else np.nan
        frac = lat_mean / a_mean if a_mean > 0 and np.isfinite(lat_mean) else np.nan
        yaw_ep = np.where(np.isfinite(yaw[sl]), yaw[sl], 0.0)
        yaw_net = float(np.sum(np.r_[0, np.diff(t[sl])] * yaw_ep))  # deg
        dv = float(np.nanmean(dvdt[sl]))
        v_mean = float(np.nanmean(v[sl]))
        if not np.isfinite(frac):
            kind = "low-speed" if v_mean < 3.0 else "unknown"
        elif frac >= STEER_FRAC:
            kind = "turn" if abs(yaw_net) >= TURN_DEG else "steering (curve/lane)"
        elif frac <= PEDAL_FRAC:
            kind = "brake" if dv < -0.05 else "accelerator"
        else:
            kind = "mixed"
        rows.append({
            "t_start": t[i0], "t_end": t[i1 - 1],
            "duration_s": t[i1 - 1] - t[i0],
            "a_mean": round(a_mean, 3), "a_lat_mean": round(lat_mean, 3) if np.isfinite(lat_mean) else np.nan,
            "steer_frac": round(frac, 3) if np.isfinite(frac) else np.nan,
            "net_heading_deg": round(yaw_net, 1),
            "gps_dvdt": round(dv, 3),
            "kind": kind,
        })
    return pd.DataFrame(rows)


KIND_COLORS = {
    "accelerator": "#2ca02c", "brake": "#d62728",
    "turn": "#9467bd", "steering (curve/lane)": "#c5b0d5",
    "mixed": "#ff7f0e", "low-speed": "#e7d4a8", "unknown": "#bbbbbb",
}


def plot_trip(trip, gps, yaw, episodes: pd.DataFrame, windows) -> Path:
    tmin = trip.t / 60
    fig, (ax1, ax2, ax3) = plt.subplots(
        3, 1, figsize=(16, 9), dpi=180, sharex=True,
        gridspec_kw={"height_ratios": [3, 2, 2], "hspace": 0.10})

    v = np.interp(trip.t, gps.t, gps.v_recon_mps.fillna(0.0))
    a_lat = np.abs(v * np.radians(yaw))
    ax1.plot(tmin, trip.a_horiz, color="#1f77b4", lw=1.0, label="|a_xy| sustained")
    ax1.plot(tmin, a_lat, color="#9467bd", lw=1.0, alpha=0.85,
             label="|a_lat| = v·ω_yaw (steering-predicted)")
    for e in episodes.itertuples(index=False):
        ax1.axvspan(e.t_start / 60, e.t_end / 60,
                    color=KIND_COLORS.get(e.kind, "#bbbbbb"), alpha=0.25, lw=0)
    ax1.set_ylabel("m/s²")
    ax1.set_ylim(0, 3)
    handles = [plt.Rectangle((0, 0), 1, 1, color=c, alpha=0.4)
               for c in KIND_COLORS.values()]
    labels = [ln.get_label() for ln in ax1.get_lines()] + list(KIND_COLORS)
    ax1.legend([*ax1.get_lines(), *handles], labels,
               loc="upper right", fontsize=7, ncols=4, frameon=False)

    ax2.plot(tmin, yaw, color="#17becf", lw=0.8, label="GPS bearing rate (yaw)")
    ax2.axhline(0, color="#999999", lw=0.5)
    ax2.set_ylabel("yaw rate (deg/s)")
    ax2.set_ylim(-15, 15)
    ax2.legend(loc="upper right", fontsize=7, frameon=False)

    ax3.plot(gps.t / 60, gps.v_recon_mps * CM.MPS_TO_MPH, color="#2ca02c", lw=1.2)
    ax3.set_ylabel("speed (mph)")
    ax3.set_xlabel("minutes")

    for ax in (ax1, ax2, ax3):
        ax.grid(True, alpha=0.25, lw=0.6)
        ax.spines[["top", "right"]].set_visible(False)

    n_by = episodes.kind.value_counts().to_dict() if len(episodes) else {}
    ax1.set_title(f"{trip.key} — pedal vs steering episodes  {n_by}",
                  fontsize=11, loc="left")
    CM.FIG_DIR.mkdir(parents=True, exist_ok=True)
    out = CM.FIG_DIR / f"steering_{trip.key}.png"
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--keys", nargs="*", default=None)
    ap.add_argument("--extra-dir", nargs="*", default=[], type=Path)
    args = ap.parse_args()
    keys = args.keys or [k for k in CM.DASHBOARD_KEYS if CM.locate_trip(k, args.extra_dir)]
    if not keys:
        print("no trips with data found — download the dashboard trips first")
        return 1

    all_eps = []
    for key in keys:
        trip = CM.load_motion(key, args.extra_dir)
        try:
            gps = CM.load_gps(trip)
        except ValueError as e:
            print(f"  !! {e} — steering exploration needs GPS; skipping")
            continue
        yaw, windows = estimate_yaw(trip, gps)
        kept = [w for w in windows if w["kept"]]
        print(f"  yaw windows kept {len(kept)}/{len(windows)} "
              f"(r: {[round(w['r'], 2) for w in windows]})")
        eps = classify_episodes(trip, gps, yaw)
        eps.insert(0, "key", key)
        all_eps.append(eps)
        out = plot_trip(trip, gps, yaw, eps, windows)
        print(f"  -> {out.name}: {len(eps)} episodes "
              f"{eps.kind.value_counts().to_dict() if len(eps) else ''}")

    if all_eps:
        CM.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        df = pd.concat(all_eps, ignore_index=True)
        df.to_csv(CM.RESULTS_DIR / "steering_episodes.csv", index=False)
        print(f"\n{len(df)} episodes -> {CM.RESULTS_DIR / 'steering_episodes.csv'}")
        print(df.kind.value_counts().to_string())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
