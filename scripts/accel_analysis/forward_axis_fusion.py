"""Opportunistic forward-axis fusion: accelerometer + gyro, GPS only as anchor
truth — recover signed longitudinal / lateral bus acceleration in the rotating
phone frame.

Idea (user-proposed, opportunistic-calibration family):
 1. **Anchors.** During a clean pedal event the sustained linear-accel vector
    points along ±forward; the sign of GPS dv/dt resolves the ±. That
    measures the forward axis f̂ (phone coordinates) at that moment.
 2. **Propagation.** Between anchors, rotate f̂ against the phone's own
    rotation (low-passed gyro, small-angle Rodrigues steps) — this holds f̂
    fixed in space while the phone tumbles. GPS yaw is deliberately NOT
    folded in (pure gyro between anchors). Propagation runs forward from
    each anchor and backward from the next, blended across the gap.
 3. **Decomposition.** a_long = A_sus·f̂ (signed: + = accelerating);
    a_lat = sqrt(|A_sus|² − a_long²) (sustained accel is horizontal, so no
    vertical reference is needed for the magnitude).

Validation (held-out): wherever GPS is valid, compare
    a_long  vs  the GPS reconstruction's signed accel f''(t)
    a_lat   vs  |v·ω| from GPS path curvature
binned by time since the nearest anchor — the go/no-go curve that measures
how fast body-sway drift destroys the propagated axis.

Gyro handling per the plan: rotation rates are low-passed (default 0.4 Hz,
--gyro-lp to change) so only *sustained* rotation propagates; fast shakes
integrate to ~zero anyway but the LP keeps them out entirely.

NOTE devicemotion column order: motion.csv stores (gx,gy,gz) =
rotationRate.(alpha,beta,gamma) = rotation about the phone's (z,x,y) axes.
The ω vector used here is reordered to (about x, about y, about z).

    PYTHONPATH=src uv run python scripts/accel_analysis/forward_axis_fusion.py
        [--keys ...] [--gyro-lp 0.4] [--omega-sign +1|-1|auto]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import common as CM  # noqa: E402
from steering_exploration import gps_bearing_rate  # noqa: E402

GYRO_LP_HZ = 0.4          # sustained-rotation band for propagation
ANCHOR_MIN_A = 0.5        # m/s² sustained |a| for an anchor window
ANCHOR_MIN_DVDT = 0.25    # m/s² GPS accel magnitude
ANCHOR_MAX_STEER = 0.30   # |v·ω_gps| / |a| must be below this (pedal-dominated)
ANCHOR_MIN_S = 1.5        # s
ANCHOR_MAX_DIR_STD = 25.0 # deg — accel direction stability within the window
VAL_MIN_AGPS = 0.2        # m/s² — only score samples with real GPS accel
BINS_S = [0, 5, 10, 20, 40, 80, 1e9]


# ------------------------------------------------------------------ anchors

def find_anchors(trip: CM.MotionTrip, gps: pd.DataFrame) -> pd.DataFrame:
    """Pedal-dominated windows -> one f̂ measurement each (phone frame)."""
    t = trip.t
    v = np.interp(t, gps.t, gps.v_recon_mps.fillna(0.0))
    dvdt = np.interp(t, gps.t, gps.a_recon_ms2.fillna(0.0))
    tg, w_gps, _ = gps_bearing_rate(gps)
    a_lat_gps = np.abs(v * np.radians(np.interp(t, tg, w_gps)))

    a = trip.a_horiz
    on = ((a > ANCHOR_MIN_A) & (np.abs(dvdt) > ANCHOR_MIN_DVDT)
          & (a_lat_gps < ANCHOR_MAX_STEER * np.clip(a, 1e-6, None))
          & ~trip.handling & np.isfinite(a))
    rows = []
    i = 0
    while i < len(on):
        if not on[i]:
            i += 1
            continue
        j = i
        while j < len(on) and on[j]:
            j += 1
        if t[j - 1] - t[i] >= ANCHOR_MIN_S:
            sl = slice(i, j)
            W = trip.A_sus[sl]
            wts = np.linalg.norm(W, axis=1)
            mean_vec = (W * wts[:, None]).sum(0)
            mean_vec /= max(np.linalg.norm(mean_vec), 1e-9)
            # direction stability: angular spread around the mean
            cosang = np.clip((W / np.clip(wts[:, None], 1e-9, None)) @ mean_vec, -1, 1)
            dir_std = float(np.degrees(np.std(np.arccos(cosang))))
            sign = float(np.sign(np.mean(dvdt[sl])))
            if dir_std <= ANCHOR_MAX_DIR_STD and sign != 0:
                rows.append({
                    "i0": i, "i1": j, "t_mid": float(t[(i + j) // 2]),
                    "f_hat": sign * mean_vec, "dir_std_deg": round(dir_std, 1),
                    "dvdt": float(np.mean(dvdt[sl])),
                })
        i = j
    return pd.DataFrame(rows)


# -------------------------------------------------------------- propagation

def _omega_rad(trip: CM.MotionTrip, lp_hz: float) -> np.ndarray:
    """Rotation-rate vector (rad/s) in phone-axis order (x,y,z), low-passed."""
    # motion.csv column order (alpha,beta,gamma) = about (z,x,y)
    W = np.radians(trip.G_raw[:, [1, 2, 0]])
    return CM._per_segment(trip.t, W, trip.hz, lp_hz)


def _propagate(t, omega, f0, i_from, i_to, sign):
    """Rotate f̂ by -ω per step from index i_from toward i_to (exclusive)."""
    out = np.full((abs(i_to - i_from), 3), np.nan)
    f = f0.copy()
    step = 1 if i_to > i_from else -1
    k = 0
    for i in range(i_from, i_to, step):
        j = i + step
        dt = abs(t[j] - t[i]) if 0 <= j < len(t) else 0.0
        w = omega[i]
        if np.isfinite(w).all() and dt < 1.0:      # gaps: hold (segments split anyway)
            f = f + np.cross(-sign * step * w, f) * dt
            f /= max(np.linalg.norm(f), 1e-9)
        out[k] = f
        k += 1
    return out


def propagate_axis(trip: CM.MotionTrip, anchors: pd.DataFrame,
                   lp_hz: float, omega_sign: float) -> np.ndarray:
    """f̂(t) for every motion sample: forward/backward from anchors, blended.

    Propagation never crosses a motion-stream gap (orientation unknowable
    across suspends) — anchors serve only their own contiguous segment."""
    n = len(trip.t)
    F = np.full((n, 3), np.nan)
    if not len(anchors):
        return F
    omega = _omega_rad(trip, lp_hz)
    t = trip.t

    for i0, i1 in trip.segments:
        seg_anchors = anchors[(anchors.t_mid >= t[i0]) & (anchors.t_mid <= t[i1 - 1])]
        if not len(seg_anchors):
            continue
        fwd = np.full((i1 - i0, 3), np.nan)
        bwd = np.full((i1 - i0, 3), np.nan)
        wgt = np.full(i1 - i0, np.nan)             # blend weight toward bwd
        arts = seg_anchors.sort_values("t_mid").reset_index(drop=True)
        for a_i, a in enumerate(arts.itertuples(index=False)):
            c = (a.i0 + a.i1) // 2
            # anchor interior: measured axis
            fwd[a.i0 - i0: a.i1 - i0] = a.f_hat
            bwd[a.i0 - i0: a.i1 - i0] = a.f_hat
            wgt[a.i0 - i0: a.i1 - i0] = 0.5
            # forward to next anchor (or segment end)
            nxt = int((arts.iloc[a_i + 1].i0 + arts.iloc[a_i + 1].i1) // 2) \
                if a_i + 1 < len(arts) else i1 - 1
            fwd[c - i0: nxt - i0] = _propagate(t, omega, np.asarray(a.f_hat), c, nxt, omega_sign)
            # backward to previous anchor (or segment start)
            prv = int((arts.iloc[a_i - 1].i0 + arts.iloc[a_i - 1].i1) // 2) \
                if a_i - 1 >= 0 else i0
            bwd[prv - i0: c - i0] = _propagate(t, omega, np.asarray(a.f_hat), c, prv, omega_sign)[::-1]
        # blend weights: linear in time between consecutive anchor centers
        centers = ((arts.i0 + arts.i1) // 2).to_numpy()
        tc = t[centers]
        idx = np.searchsorted(tc, t[i0:i1])
        w = np.zeros(i1 - i0)
        inside = (idx > 0) & (idx < len(tc))
        lo = tc[np.clip(idx - 1, 0, len(tc) - 1)]
        hi = tc[np.clip(idx, 0, len(tc) - 1)]
        with np.errstate(invalid="ignore", divide="ignore"):
            w[inside] = ((t[i0:i1] - lo) / np.clip(hi - lo, 1e-9, None))[inside]
        w[idx == 0] = 1.0                          # before first anchor: only bwd
        w[idx == len(tc)] = 0.0                    # after last anchor: only fwd
        both = np.isfinite(fwd[:, 0]) & np.isfinite(bwd[:, 0])
        only_f = np.isfinite(fwd[:, 0]) & ~both
        only_b = np.isfinite(bwd[:, 0]) & ~both
        blend = np.full((i1 - i0, 3), np.nan)
        blend[both] = (1 - w[both, None]) * fwd[both] + w[both, None] * bwd[both]
        blend[only_f] = fwd[only_f]
        blend[only_b] = bwd[only_b]
        norm = np.linalg.norm(blend, axis=1, keepdims=True)
        F[i0:i1] = blend / np.clip(norm, 1e-9, None)
    return F


# -------------------------------------------------------------- validation

def decompose(trip: CM.MotionTrip, F: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    a_long = np.einsum("ij,ij->i", trip.A_sus, F)
    a_lat = np.sqrt(np.clip(trip.a_horiz ** 2 - a_long ** 2, 0, None))
    return a_long, a_lat


def validate(trip, gps, anchors, a_long, a_lat) -> tuple[dict, pd.DataFrame]:
    t = trip.t
    v = np.interp(t, gps.t, gps.v_recon_mps.fillna(0.0))
    a_gps = np.interp(t, gps.t, gps.a_recon_ms2.fillna(0.0))
    tg, w_gps, moving_g = gps_bearing_rate(gps)
    lat_gps = np.abs(v * np.radians(np.interp(t, tg, w_gps)))
    mv = np.interp(t, tg, moving_g.astype(float)) > 0.5

    t_anch = anchors.t_mid.to_numpy()
    dt_anchor = np.abs(t[:, None] - t_anch[None, :]).min(1) if len(t_anch) else np.full(len(t), np.inf)
    # exclude the anchor windows themselves (they'd score perfectly by construction)
    in_anchor = np.zeros(len(t), bool)
    for a in anchors.itertuples(index=False):
        in_anchor[a.i0: a.i1] = True

    ok = (np.isfinite(a_long) & ~trip.handling & mv & ~in_anchor
          & (np.abs(a_gps) > VAL_MIN_AGPS))
    res = {"n_val": int(ok.sum())}
    if ok.sum() > 100:
        res["r_long"] = float(np.corrcoef(a_long[ok], a_gps[ok])[0, 1])
        res["sign_agree"] = float((np.sign(a_long[ok]) == np.sign(a_gps[ok])).mean())
        ok_lat = ok & (lat_gps > 0.1)
        res["r_lat"] = (float(np.corrcoef(a_lat[ok_lat], lat_gps[ok_lat])[0, 1])
                        if ok_lat.sum() > 100 else np.nan)
    rows = []
    for lo, hi in zip(BINS_S[:-1], BINS_S[1:]):
        m = ok & (dt_anchor >= lo) & (dt_anchor < hi)
        if m.sum() < 100:
            continue
        rows.append({
            "bin_lo_s": lo, "bin_hi_s": min(hi, 1e6), "n": int(m.sum()),
            "r_long": float(np.corrcoef(a_long[m], a_gps[m])[0, 1]),
            "sign_agree": float((np.sign(a_long[m]) == np.sign(a_gps[m])).mean()),
        })
    return res, pd.DataFrame(rows)


# ------------------------------------------------------------------ figure

TURN_DPS = 4.0        # sustained GPS yaw above this = a turn event
TURN_MIN_S = 2.0


def turn_intervals(gps: pd.DataFrame) -> list[tuple[float, float]]:
    """(t0, t1) spans of sustained GPS-yaw turning, for visual overlay."""
    tg, w_gps, moving = gps_bearing_rate(gps)
    on = (np.abs(w_gps) > TURN_DPS) & moving
    out = []
    i = 0
    while i < len(on):
        if not on[i]:
            i += 1
            continue
        j = i
        while j < len(on) and on[j]:
            j += 1
        if tg[j - 1] - tg[i] >= TURN_MIN_S:
            out.append((float(tg[i]), float(tg[j - 1])))
        i = j
    return out


def plot_trip(trip, gps, anchors, a_long, a_lat, res, key):
    t = trip.t
    tmin = t / 60
    v = gps.v_recon_mps.fillna(0.0)
    tg, w_gps, _ = gps_bearing_rate(gps)
    lat_gps = np.abs(v.to_numpy() * np.radians(w_gps))

    fig, (ax1, ax2, ax3) = plt.subplots(
        3, 1, figsize=(16, 9), dpi=180, sharex=True,
        gridspec_kw={"height_ratios": [3, 2, 2], "hspace": 0.10})
    ax1.plot(tmin, a_long, color="#1f77b4", lw=0.9, label="a_long (fusion, signed)")
    ax1.plot(gps.t / 60, gps.a_recon_ms2, color="#ff7f0e", lw=1.2, alpha=0.9,
             label="GPS accel f'' (signed)")
    ax1.axhline(0, color="#999999", lw=0.5)
    ax1.set_ylabel("m/s²")
    ax1.set_ylim(-2.5, 2.5)
    ax1.legend(loc="upper right", fontsize=8, frameon=False, ncols=2)

    turns = turn_intervals(gps)
    for t0, t1 in turns:
        ax2.axvspan(t0 / 60, t1 / 60, color="#e0b410", alpha=0.30, lw=0, zorder=0)
    ax2.plot(tmin, a_lat, color="#9467bd", lw=0.9, label="a_lat (fusion, magnitude)")
    ax2.plot(gps.t / 60, lat_gps, color="#17becf", lw=1.1, alpha=0.9,
             label="|v·ω| (GPS curvature)")
    ax2.set_ylabel("m/s²")
    ax2.set_ylim(0, 2.5)
    handles, labels = ax2.get_legend_handles_labels()
    handles.append(plt.Rectangle((0, 0), 1, 1, color="#e0b410", alpha=0.35))
    labels.append(f"GPS turn events (|ω|>{TURN_DPS:.0f}°/s ≥{TURN_MIN_S:.0f}s, n={len(turns)})")
    ax2.legend(handles, labels, loc="upper right", fontsize=8, frameon=False, ncols=3)

    ax3.plot(gps.t / 60, v * CM.MPS_TO_MPH, color="#2ca02c", lw=1.2)
    for a in anchors.itertuples(index=False):
        ax3.axvspan(t[a.i0] / 60, t[a.i1 - 1] / 60, color="#d62728", alpha=0.25, lw=0)
    ax3.set_ylabel("speed (mph)\nred = anchors")
    ax3.set_xlabel("minutes")

    for ax in (ax1, ax2, ax3):
        ax.grid(True, alpha=0.25, lw=0.6)
        ax.spines[["top", "right"]].set_visible(False)
    ax1.set_title(
        f"{key} — forward-axis fusion · {len(anchors)} anchors · "
        f"r_long={res.get('r_long', float('nan')):.2f} "
        f"sign-agree={100 * res.get('sign_agree', float('nan')):.0f}% "
        f"r_lat={res.get('r_lat', float('nan')):.2f}",
        fontsize=11, loc="left")
    CM.FIG_DIR.mkdir(parents=True, exist_ok=True)
    out = CM.FIG_DIR / f"fusion_{key}.png"
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    return out


def plot_turn_gallery(trip, gps, a_lat, key, pad_s: float = 20.0):
    """One zoomed panel per GPS turn event: does a_lat rise inside the turn?"""
    turns = turn_intervals(gps)
    if not turns:
        return None
    tg, w_gps, _ = gps_bearing_rate(gps)
    lat_gps = np.abs(gps.v_recon_mps.fillna(0).to_numpy() * np.radians(w_gps))
    n = len(turns)
    ncol = min(5, n)
    nrow = int(np.ceil(n / ncol))
    fig, axes = plt.subplots(nrow, ncol, figsize=(3.2 * ncol, 2.4 * nrow),
                             dpi=170, squeeze=False, sharey=True)
    for ax in axes.flat[n:]:
        ax.axis("off")
    for k, (t0, t1) in enumerate(turns):
        ax = axes.flat[k]
        lo, hi = t0 - pad_s, t1 + pad_s
        m = (trip.t >= lo) & (trip.t <= hi)
        mg = (tg >= lo) & (tg <= hi)
        ax.axvspan(t0, t1, color="#e0b410", alpha=0.30, lw=0)
        ax.plot(trip.t[m], a_lat[m], color="#9467bd", lw=1.0)
        ax.plot(tg[mg], lat_gps[mg], color="#17becf", lw=1.1, alpha=0.9)
        ax.set_ylim(0, 2.2)
        ax.set_title(f"turn {k + 1} @ {t0 / 60:.1f} min ({t1 - t0:.0f} s)", fontsize=8)
        ax.grid(True, alpha=0.25, lw=0.5)
        ax.spines[["top", "right"]].set_visible(False)
        ax.tick_params(labelsize=7)
    fig.suptitle(f"{key} — a_lat (purple) vs GPS |v·ω| (cyan) at each GPS turn event "
                 f"(yellow = turn, ±{pad_s:.0f} s context)", fontsize=10, x=0.01, ha="left")
    fig.tight_layout(rect=(0, 0, 1, 0.95))
    out = CM.FIG_DIR / f"turns_{key}.png"
    fig.savefig(out, bbox_inches="tight")
    plt.close(fig)
    return out


def turn_contrast(trip, gps, a_lat) -> dict:
    """Mean a_lat inside GPS turn events vs outside (moving, valid, no anchor bias)."""
    turns = turn_intervals(gps)
    t = trip.t
    v = np.interp(t, gps.t, gps.v_recon_mps.fillna(0.0))
    in_turn = np.zeros(len(t), bool)
    for t0, t1 in turns:
        in_turn |= (t >= t0) & (t <= t1)
    ok = np.isfinite(a_lat) & ~trip.handling & (v > 3.0)
    inside = a_lat[ok & in_turn]
    outside = a_lat[ok & ~in_turn]
    if len(inside) < 30 or len(outside) < 30:
        return {}
    return {
        "n_turns": len(turns),
        "a_lat_in_turn": round(float(inside.mean()), 3),
        "a_lat_outside": round(float(outside.mean()), 3),
        "turn_contrast": round(float(inside.mean() / max(outside.mean(), 1e-9)), 2),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--keys", nargs="*", default=None)
    ap.add_argument("--extra-dir", nargs="*", default=[], type=Path)
    ap.add_argument("--gyro-lp", type=float, default=GYRO_LP_HZ)
    ap.add_argument("--omega-sign", default="auto", choices=["auto", "+1", "-1"])
    args = ap.parse_args()
    keys = args.keys or [k for k in CM.DASHBOARD_KEYS if CM.locate_trip(k, args.extra_dir)]

    stats, bins_all = [], []
    for key in keys:
        trip = CM.load_motion(key, args.extra_dir)
        try:
            gps = CM.load_gps(trip)
        except ValueError as e:
            print(f"  !! {e} — skipping")
            continue
        anchors = find_anchors(trip, gps)
        if len(anchors) < 3:
            print(f"  !! only {len(anchors)} anchors — skipping")
            continue
        gap = np.diff(anchors.t_mid.to_numpy())
        print(f"  {len(anchors)} anchors (median gap {np.median(gap):.0f}s, "
              f"max {gap.max():.0f}s)")

        # ω sign convention: decided empirically on the first trip when auto.
        if args.omega_sign == "auto":
            best = None
            for s in (+1.0, -1.0):
                F = propagate_axis(trip, anchors, args.gyro_lp, s)
                a_long, a_lat = decompose(trip, F)
                res, _ = validate(trip, gps, anchors, a_long, a_lat)
                print(f"    ω sign {s:+.0f}: r_long={res.get('r_long', float('nan')):.3f}")
                if best is None or res.get("r_long", -9) > best[1].get("r_long", -9):
                    best = (s, res, F, a_long, a_lat)
            omega_sign, res, F, a_long, a_lat = best[0], best[1], best[2], best[3], best[4]
            args.omega_sign = f"{omega_sign:+.0f}"   # lock for remaining trips
        else:
            omega_sign = float(args.omega_sign)
            F = propagate_axis(trip, anchors, args.gyro_lp, omega_sign)
            a_long, a_lat = decompose(trip, F)
            res, _ = validate(trip, gps, anchors, a_long, a_lat)

        res_full, bins = validate(trip, gps, anchors, a_long, a_lat)
        res_full.update(turn_contrast(trip, gps, a_lat))
        bins.insert(0, "key", key)
        bins_all.append(bins)
        plot_turn_gallery(trip, gps, a_lat, key)
        out = plot_trip(trip, gps, anchors, a_long, a_lat, res_full, key)
        print(f"  -> {out.name}: r_long={res_full.get('r_long', float('nan')):.2f} "
              f"sign={100 * res_full.get('sign_agree', float('nan')):.0f}% "
              f"r_lat={res_full.get('r_lat', float('nan')):.2f}")
        stats.append({"key": key, "n_anchors": len(anchors),
                      "median_gap_s": round(float(np.median(gap)), 1),
                      "omega_sign": omega_sign, **{k: (round(v, 3) if isinstance(v, float) else v)
                                                   for k, v in res_full.items()}})

    if stats:
        CM.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(stats).to_csv(CM.RESULTS_DIR / "fusion_stats.csv", index=False)
        bins_df = pd.concat(bins_all, ignore_index=True)
        bins_df.to_csv(CM.RESULTS_DIR / "fusion_drift_bins.csv", index=False)
        pooled = bins_df.groupby(["bin_lo_s", "bin_hi_s"]).apply(
            lambda g: pd.Series({
                "n": g.n.sum(),
                "sign_agree_wmean": np.average(g.sign_agree, weights=g.n),
                "r_long_wmean": np.average(g.r_long, weights=g.n),
            }), include_groups=False)
        print("\n=== drift vs time-since-anchor (pooled) ===")
        print(pooled.round(3).to_string())
        print(pd.DataFrame(stats).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
