"""Downsample -> reconstruct -> score, per trip.

Feed construction: the downsampled feed at cadence ``f`` keeps the trip's
pings whose ``stream_idx % (f/2) == 0`` — i.e. every N-th row of the
vehicle's continuous 2 s stream (index 0 kept), trimmed to the trip window.

Scoring: every reconstruction is evaluated on the trip's common 1 s grid
(the intersection of all feeds' knot spans) against the baseline truth
defined in ``config.BASELINE`` (default VCHIP-ME on the full 2 s feed).

Per (trip, freq, method) the scorer emits components for:

M1   position RMSE/MAE vs baseline (m)
M2   speed RMSE/MAE vs baseline (m/s)
M3   door-open seconds below 1/3/5/7.5/10 mph (AVL door events)
M3b  |reconstructed position at mid-dwell - projected AVL door location| (m)
M4   seconds with acceleration inside tight/loose bounds (paper Table 3)
M5   signal-zone (300 ft upstream -> signal) travel-time error vs baseline
M6   slow-state (v < 5 mph) TP/FP/FN vs baseline  -> pooled F1
M7   slowdown events (>= 10 s) greedy-matched vs baseline -> pooled F1
"""

from __future__ import annotations

import zlib

import numpy as np
import pandas as pd

import config as C
import methods as M


def epoch_s(dt: pd.Series) -> np.ndarray:
    # normalize to ns first — parquet round-trips can yield ms/us-unit dtypes
    return dt.astype("datetime64[ns]").astype("int64").to_numpy() / 1e9


def slow_events(mask: np.ndarray, dt: float = 1.0) -> list[tuple[float, float]]:
    """Contiguous slow runs >= MIN_EVENT_S, as (start, end) grid offsets."""
    if not mask.any():
        return []
    idx = np.flatnonzero(np.diff(np.concatenate(([0], mask.view(np.int8), [0]))))
    runs = idx.reshape(-1, 2)  # [start, end) index pairs
    return [(float(a * dt), float(b * dt)) for a, b in runs
            if (b - a) * dt >= C.MIN_EVENT_S]


def match_events(base: list, rec: list) -> int:
    """Greedy 1:1 matching by temporal overlap >= 50% of the shorter event."""
    pairs = []
    for i, (b0, b1) in enumerate(base):
        for j, (r0, r1) in enumerate(rec):
            ov = min(b1, r1) - max(b0, r0)
            if ov >= C.EVENT_MATCH_MIN_OVERLAP * min(b1 - b0, r1 - r0):
                pairs.append((ov, i, j))
    pairs.sort(reverse=True)
    used_b, used_r, n = set(), set(), 0
    for _, i, j in pairs:
        if i not in used_b and j not in used_r:
            used_b.add(i)
            used_r.add(j)
            n += 1
    return n


class TripEval:
    """Holds one trip's feeds, common grid, baseline truth, and AVL truth."""

    def __init__(self, pings: pd.DataFrame, doors: pd.DataFrame,
                 signal_x: np.ndarray, trip_key: str = ""):
        t_all = epoch_s(pings["ping_dt"])
        x_all = pings["x_m"].to_numpy(dtype=float)
        v_all = np.nan_to_num(pings["v_mps"].to_numpy(dtype=float), nan=0.0)
        idx = pings["stream_idx"].to_numpy()

        self.feeds: dict[int, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
        for f in C.FREQS_S:
            sel = (idx % C.stride_for(f)) == 0
            if sel.sum() >= 2:
                self.feeds[f] = (t_all[sel], x_all[sel], v_all[sel])

        # paper-style holdout: one common random 5% of the trip's 2 s pings
        # (stable per trip), removed from every feed's knots for the M1/M2
        # holdout scoring and used as measured-truth evaluation points
        rng = np.random.default_rng(
            (C.HOLDOUT_SEED, zlib.crc32(trip_key.encode())))
        held = np.zeros(len(pings), dtype=bool)
        n_hold = int(round(len(pings) * C.HOLDOUT_FRACTION))
        if n_hold:
            held[rng.choice(len(pings), size=n_hold, replace=False)] = True
        self.ho_t, self.ho_x, self.ho_v = t_all[held], x_all[held], v_all[held]
        self.feeds_ho: dict[int, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
        for f in C.FREQS_S:
            sel = ((idx % C.stride_for(f)) == 0) & ~held
            if sel.sum() >= 2:
                self.feeds_ho[f] = (t_all[sel], x_all[sel], v_all[sel])

        t0 = max(t[0] for t, _, _ in self.feeds.values())
        t1 = min(t[-1] for t, _, _ in self.feeds.values())
        self.grid = np.arange(np.ceil(t0), np.floor(t1) + 0.5, C.DENSE_DT_S)
        if len(self.grid) < 60:
            return

        b_method, b_freq, b_params = C.BASELINE
        tb, xb, vb = self.feeds[b_freq]
        H = M.BUILDERS[b_method](tb, xb, vb, **b_params)
        self.x_base = H.pos(self.grid)
        self.v_base = H.vel(self.grid)
        base_mph = self.v_base * C.MPS_TO_MPH
        self.slow_base = base_mph < C.SLOW_MPH
        self.events_base = slow_events(self.slow_base, C.DENSE_DT_S)

        # ---- M5: signal zones traversed inside the common grid
        xg0, xg1 = self.x_base[0], self.x_base[-1]
        zones = signal_x[(signal_x - C.SIGNAL_ZONE_M >= xg0) & (signal_x <= xg1)]
        self.zone_x = zones
        self.tt_base = self._zone_tt(self.x_base, zones)

        # ---- M3/M3b: door events (dwell-bounded)
        self.door_masks: list[np.ndarray] = []
        self.door_mid_t = np.array([])
        self.door_x = np.array([])
        if len(doors):
            dw = doors["dwell_s"].to_numpy(dtype=float)
            e0 = epoch_s(doors["event_dt"])
            xd = doors["x_door_m"].to_numpy(dtype=float)
            ok = (dw >= C.MIN_DOOR_DWELL_S) & (dw <= C.MAX_DOOR_DWELL_S)
            for s, d in zip(e0[ok], dw[ok]):
                m = (self.grid >= s) & (self.grid < s + d)
                if m.any():
                    self.door_masks.append(m)
            mid = e0 + dw / 2
            loc_ok = ok & np.isfinite(xd) & (mid >= self.grid[0]) & (mid <= self.grid[-1])
            self.door_mid_t = mid[loc_ok]
            self.door_x = xd[loc_ok]
        self.door_total_s = int(sum(m.sum() for m in self.door_masks))

    def _zone_tt(self, xg: np.ndarray, zones: np.ndarray) -> np.ndarray:
        """Travel time through [x_sig - ZONE, x_sig] from monotone xg."""
        if not len(zones):
            return np.array([])
        x = np.maximum.accumulate(xg)  # guard fp wiggle; methods are monotone
        t_in = np.interp(zones - C.SIGNAL_ZONE_M, x, self.grid)
        t_out = np.interp(zones, x, self.grid)
        return t_out - t_in

    def usable(self) -> bool:
        return (len(self.feeds) == len(C.FREQS_S)
                and len(self.feeds_ho) == len(C.FREQS_S)
                and len(self.grid) >= 60)

    def score_holdout(self, freq: int, method: str, params: dict) -> dict:
        """M1/M2 paper-style: reconstruct from the feed minus held-out pings,
        evaluate against MEASURED x/v at the held-out points (within the
        common grid span, so every frequency is scored at the same points)."""
        t, x, v = self.feeds_ho[freq]
        return self.score_holdout_arrays(t, x, v, method, params)

    def score_holdout_arrays(self, t, x, v, method: str, params: dict) -> dict:
        """Holdout scoring for an EXTERNAL feed (e.g. the real AVL archive):
        its own pings are the knots; the held-out VTRAK pings are never among
        them, so they serve as unseen measured-truth points directly."""
        if v is None:
            v = np.zeros_like(x)
        H = M.BUILDERS[method](t, x, v, **params)
        inside = ((self.ho_t >= max(t[0], self.grid[0]))
                  & (self.ho_t <= min(t[-1], self.grid[-1])))
        th, xh, vh = self.ho_t[inside], self.ho_x[inside], self.ho_v[inside]
        if not len(th):
            return {"ho_n": 0}
        ex = H.pos(th) - xh
        ev = H.vel(th) - vh
        return {
            "ho_n": int(len(th)),
            "ho_mae_x": float(np.mean(np.abs(ex))),
            "ho_rmse_x": float(np.sqrt(np.mean(ex**2))),
            "ho_mae_v": float(np.mean(np.abs(ev))),
            "ho_rmse_v": float(np.sqrt(np.mean(ev**2))),
        }

    def score(self, freq: int, method: str, params: dict) -> dict:
        t, x, v = self.feeds[freq]
        return self.score_arrays(t, x, v, method, params)

    def score_arrays(self, t: np.ndarray, x: np.ndarray, v: np.ndarray | None,
                     method: str, params: dict) -> dict:
        """Score any (t, x[, v]) feed — used for the ladder and the real R2 feed."""
        if v is None:
            v = np.zeros_like(x)
        H = M.BUILDERS[method](t, x, v, **params)
        xp = H.pos(self.grid)
        vp = H.vel(self.grid)
        vp_mph = vp * C.MPS_TO_MPH
        dx = xp - self.x_base
        dv = vp - self.v_base
        out = {
            "rmse_x": float(np.sqrt(np.mean(dx**2))),
            "mae_x": float(np.mean(np.abs(dx))),
            "rmse_v": float(np.sqrt(np.mean(dv**2))),
            "mae_v": float(np.mean(np.abs(dv))),
            "n_knots": len(t),
            "grid_s": len(self.grid),
        }

        # M3 door-open stopped seconds per threshold (ft/s, as in the paper)
        out["door_total_s"] = self.door_total_s
        vp_ftps = vp * C.MPS_TO_FTPS
        for th in (C.STOP_FTPS_MAIN, *C.STOP_FTPS_WHISKERS):
            out[f"door_stop_{th}"] = int(
                sum((vp_ftps[m] < th).sum() for m in self.door_masks))

        # M3b door location error
        if len(self.door_mid_t):
            x_rec = np.interp(self.door_mid_t, self.grid, np.maximum.accumulate(xp))
            err = np.abs(x_rec - self.door_x)
            out["doorloc_abs_err_sum"] = float(err.sum())
            out["doorloc_n"] = int(len(err))
        else:
            out["doorloc_abs_err_sum"] = 0.0
            out["doorloc_n"] = 0

        # M4 acceleration within bounds
        acc = np.diff(vp) / C.DENSE_DT_S
        out["n_accel"] = len(acc)
        out["accel_ok_tight"] = int(
            ((acc >= C.ACCEL_TIGHT[0]) & (acc <= C.ACCEL_TIGHT[1])).sum())
        out["accel_ok_loose"] = int(
            ((acc >= C.ACCEL_LOOSE[0]) & (acc <= C.ACCEL_LOOSE[1])).sum())

        # M5 signal-zone travel time vs baseline
        tt_rec = self._zone_tt(xp, self.zone_x)
        ok = self.tt_base > 0
        out["zone_n"] = int(ok.sum())
        out["zone_abs_err_sum"] = float(np.abs(tt_rec[ok] - self.tt_base[ok]).sum())
        out["zone_tt_base_sum"] = float(self.tt_base[ok].sum())

        # M6 slow-state confusion vs baseline
        slow_rec = vp_mph < C.SLOW_MPH
        out["slow_tp"] = int((self.slow_base & slow_rec).sum())
        out["slow_fp"] = int((~self.slow_base & slow_rec).sum())
        out["slow_fn"] = int((self.slow_base & ~slow_rec).sum())

        # M7 slowdown events vs baseline
        ev_rec = slow_events(slow_rec, C.DENSE_DT_S)
        out["ev_base"] = len(self.events_base)
        out["ev_rec"] = len(ev_rec)
        out["ev_match"] = match_events(self.events_base, ev_rec)
        return out
