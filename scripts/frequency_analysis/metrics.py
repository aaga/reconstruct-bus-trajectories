"""Agreement metrics: downsampled reconstruction vs. the 2 s benchmark.

Seven metrics at deliberately different altitudes of the pipeline:

  M1  position   — % of common seconds with |Δx| <= 50 m
  M2  speed      — % of common seconds with |Δv| <= 2.5 mph
  M3  slow-state — per-second F1 of the raw v<5 mph mask (delay *detection*)
  M4  events     — event-level F1 of detected slowdown events (delay *events*)
  M5  attribution— weighted Jaccard of delay seconds by (category, facility)
  M6  door-speed — % of AVL door-open seconds reconstructed at v<5 mph
                   (external ground truth: the bus IS stopped then)
  M7  dwell-recall— % of AVL serviced-stop dwells (event 3) recovered as a
                   "dwell" attribution at that same stop (external ground truth)

M1–M5 compare against the benchmark reconstruction; M6–M7 compare against the
AVL door data and are therefore also imperfect at the benchmark itself — the
curves are normalized to benchmark=100% downstream, raw values kept.

Every function returns pooled *components* (counts/sums) so trips can be
aggregated by summation (micro-average) rather than averaging ratios.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

import config as C  # noqa: E402

DWELL_TIME_TOL_S = 30.0  # M7: temporal slack around the AVL door interval


# ------------------------------------------------------------- grid overlap

def common_grid(bench, var):
    """Indices into bench.t / var.t for their common integer-second span."""
    t_lo = max(bench.t[0], var.t[0])
    t_hi = min(bench.t[-1], var.t[-1])
    if t_hi <= t_lo:
        return None
    bi = slice(int(t_lo - bench.t[0]), int(t_hi - bench.t[0]) + 1)
    vi = slice(int(t_lo - var.t[0]), int(t_hi - var.t[0]) + 1)
    return t_lo, t_hi, bi, vi


# ------------------------------------------------------------- M1 + M2

def grid_metrics(bench, var) -> dict:
    g = common_grid(bench, var)
    if g is None:
        return {}
    t_lo, t_hi, bi, vi = g
    dx = np.abs(var.x[vi] - bench.x[bi])
    dv = np.abs(var.v_mph[vi] - bench.v_mph[bi])
    n = dx.size
    return {
        "n_sec": n,
        "pos_ok": int((dx <= C.POS_TOL_M).sum()),
        "pos_abs_sum": float(dx.sum()),
        "pos_sq_sum": float((dx ** 2).sum()),
        "spd_ok": int((dv <= C.SPEED_TOL_MPH).sum()),
        "spd_abs_sum": float(dv.sum()),
        "spd_sq_sum": float((dv ** 2).sum()),
    }


# ------------------------------------------------------------- M3

def slow_state_metrics(bench, var) -> dict:
    g = common_grid(bench, var)
    if g is None:
        return {}
    _, _, bi, vi = g
    b = bench.v_mph[bi] < C.SLOW_MPH
    v = var.v_mph[vi] < C.SLOW_MPH
    return {
        "tp": int((b & v).sum()),
        "fp": int((~b & v).sum()),
        "fn": int((b & ~v).sum()),
    }


# ------------------------------------------------------------- M4

def _overlap(a0, a1, b0, b1) -> float:
    return max(0.0, min(a1, b1) - max(a0, b0))


def event_metrics(bench, var) -> dict:
    """Greedy 1:1 matching of detected slowdown events by temporal overlap."""
    g = common_grid(bench, var)
    if g is None:
        return {}
    t_lo, t_hi = g[0], g[1]

    def inside(evs):
        return [e for e in evs
                if _overlap(e.t_start, e.t_end, t_lo, t_hi) >= 0.5 * e.duration_s]

    be, ve = inside(bench.events), inside(var.events)
    pairs = []
    for i, b in enumerate(be):
        for j, v in enumerate(ve):
            ov = _overlap(b.t_start, b.t_end, v.t_start, v.t_end)
            if ov >= C.EVENT_MATCH_MIN_OVERLAP * min(b.duration_s, v.duration_s):
                pairs.append((ov, i, j))
    pairs.sort(reverse=True)
    used_b, used_v = set(), set()
    n_match = 0
    for _, i, j in pairs:
        if i in used_b or j in used_v:
            continue
        used_b.add(i)
        used_v.add(j)
        n_match += 1
    return {"n_bench_ev": len(be), "n_var_ev": len(ve), "n_match_ev": n_match}


# ------------------------------------------------------------- M5

def _attr_seconds(attrs: list[dict], t_lo: float, t_hi: float) -> dict[tuple, float]:
    out: dict[tuple, float] = {}
    for a in attrs:
        dur = _overlap(a["t_start"], a["t_end"], t_lo, t_hi)
        if dur <= 0:
            continue
        key = (a["category"], a["facility_id"] or "")
        out[key] = out.get(key, 0.0) + dur
    return out


def attribution_metrics(bench, var) -> dict:
    """Weighted Jaccard over delay seconds keyed by (category, facility)."""
    g = common_grid(bench, var)
    if g is None:
        return {}
    t_lo, t_hi = g[0], g[1]
    b = _attr_seconds(bench.attributions, t_lo, t_hi)
    v = _attr_seconds(var.attributions, t_lo, t_hi)
    keys = set(b) | set(v)
    s_min = sum(min(b.get(k, 0.0), v.get(k, 0.0)) for k in keys)
    s_max = sum(max(b.get(k, 0.0), v.get(k, 0.0)) for k in keys)
    return {"attr_min_sum": float(s_min), "attr_max_sum": float(s_max)}


# ------------------------------------------------------------- door intervals

def door_intervals(trip, min_dwell_s: float, types=("3", "4", "5")) -> pd.DataFrame:
    """AVL door-open intervals in trip-time seconds: [open_s, close_s]."""
    d = trip.door_events
    d = d[d["event_type"].isin(types)
          & (d["dwell_s"] >= min_dwell_s)
          & (d["dwell_s"] <= C.MAX_DOOR_DWELL_S)].copy()
    if d.empty:
        return pd.DataFrame(columns=["open_s", "close_s", "stop_id", "event_type"])
    d["open_s"] = (d["event_dt"] - trip.t0).dt.total_seconds()
    d["close_s"] = d["open_s"] + d["dwell_s"]
    return d[["open_s", "close_s", "stop_id", "event_type"]].reset_index(drop=True)


# ------------------------------------------------------------- M6

def door_speed_metrics(bench, var, trip) -> dict:
    """During AVL door-open time the bus is physically stopped; count how many
    of those seconds each reconstruction places below the 5 mph threshold.

    The door set is restricted to the benchmark∩variant common span so every
    level is graded on the identical target seconds.
    """
    g = common_grid(bench, var)
    if g is None:
        return {}
    t_lo, t_hi = g[0], g[1]
    doors = door_intervals(trip, C.MIN_DOOR_DWELL_S)
    n_target = 0
    n_slow = 0
    for r in doors.itertuples(index=False):
        lo, hi = max(r.open_s, t_lo), min(r.close_s, t_hi)
        if hi - lo < C.MIN_DOOR_DWELL_S:  # keep only solidly-covered intervals
            continue
        i0 = int(np.ceil(lo - var.t[0]))
        i1 = int(np.floor(hi - var.t[0]))
        seg = var.v_mph[i0:i1 + 1]
        n_target += seg.size
        n_slow += int((seg < C.SLOW_MPH).sum())
    return {"door_sec": n_target, "door_slow": n_slow}


# ------------------------------------------------------------- M7

PRECISION_MIN_DOOR_S = 5.0  # truth set for precision: any door activity >= 5 s


def dwell_recall_metrics(bench, var, trip) -> dict:
    """M7 both ways.

    Recall — of the AVL *serviced stop* events (type 3, 10 s <= dwell <=
    180 s), how many does the variant recover as a dwell attribution at the
    same stop_id overlapping the door interval (±30 s)? Temporal-only matches
    (any-stop) are kept as a stop-id-misalignment diagnostic.

    Precision — of the variant's claimed dwell attributions, how many are
    backed by real door activity at that same stop: any AVL event type 3/4/5
    with dwell >= 5 s overlapping the attribution (±30 s)? The truth set is
    deliberately broader than recall's target set (short and unserviced door
    events count) — matching those means the dwell was not *invented*, even
    though recall would never demand it. Unmatched near-side dwells are
    counted separately (the "invented near-side dwell" hypothesis).
    """
    g = common_grid(bench, var)
    if g is None:
        return {}
    t_lo, t_hi = g[0], g[1]
    dwell_attrs = [a for a in var.attributions
                   if a["category"] == "dwell"
                   and _overlap(a["t_start"], a["t_end"], t_lo, t_hi) > 0]

    # ---- recall
    doors = door_intervals(trip, C.MIN_DOOR_DWELL_S, types=("3",))
    doors = doors[(doors.stop_id != "") & (doors.stop_id != "None")]
    n_target = n_id = n_time = 0
    for r in doors.itertuples(index=False):
        if r.open_s < t_lo or r.close_s > t_hi:
            continue
        n_target += 1
        lo, hi = r.open_s - DWELL_TIME_TOL_S, r.close_s + DWELL_TIME_TOL_S
        overlapping = [a for a in dwell_attrs if _overlap(a["t_start"], a["t_end"], lo, hi) > 0]
        if overlapping:
            n_time += 1
        if any(str(a["facility_id"]) == str(r.stop_id) for a in overlapping):
            n_id += 1

    # ---- precision
    acts = door_intervals(trip, PRECISION_MIN_DOOR_S, types=("3", "4", "5"))
    acts = acts[(acts.stop_id != "") & (acts.stop_id != "None")]
    act_rows = list(acts.itertuples(index=False))
    n_pred = n_pred_ok = n_pred_near = n_fp_near = 0
    for a in dwell_attrs:
        n_pred += 1
        near = bool(a.get("dwell_near_signal"))
        if near:
            n_pred_near += 1
        lo, hi = a["t_start"] - DWELL_TIME_TOL_S, a["t_end"] + DWELL_TIME_TOL_S
        ok = any(
            str(r.stop_id) == str(a["facility_id"])
            and _overlap(r.open_s, r.close_s, lo, hi) > 0
            for r in act_rows
        )
        if ok:
            n_pred_ok += 1
        elif near:
            n_fp_near += 1

    return {
        "dwell_targets": n_target, "dwell_match_id": n_id, "dwell_match_time": n_time,
        "dwell_pred": n_pred, "dwell_pred_ok": n_pred_ok,
        "dwell_pred_near": n_pred_near, "dwell_fp_near": n_fp_near,
    }


# ------------------------------------------------------------- all together

def compute_all(bench, var, trip) -> dict:
    out: dict = {}
    for part in (
        grid_metrics(bench, var),
        slow_state_metrics(bench, var),
        event_metrics(bench, var),
        attribution_metrics(bench, var),
        door_speed_metrics(bench, var, trip),
        dwell_recall_metrics(bench, var, trip),
    ):
        out.update(part)
    return out


# ------------------------------------------------------------- summarize

def summarize(components: pd.DataFrame) -> pd.DataFrame:
    """Pooled (micro-averaged) metric values per level from summed components."""
    g = components.groupby("level").sum(numeric_only=True)
    s = pd.DataFrame(index=g.index)
    s["M1_position_pct"] = 100 * g.pos_ok / g.n_sec
    s["M2_speed_pct"] = 100 * g.spd_ok / g.n_sec
    s["M3_slowstate_f1"] = 100 * 2 * g.tp / (2 * g.tp + g.fp + g.fn)
    s["M4_event_f1"] = 100 * 2 * g.n_match_ev / (g.n_bench_ev + g.n_var_ev)
    s["M5_attribution_jaccard"] = 100 * g.attr_min_sum / g.attr_max_sum
    s["M6_door_speed_pct"] = 100 * g.door_slow / g.door_sec
    s["M7_dwell_recall_pct"] = 100 * g.dwell_match_id / g.dwell_targets
    s["M7_dwell_recall_timeonly_pct"] = 100 * g.dwell_match_time / g.dwell_targets
    s["M7_dwell_precision_pct"] = 100 * g.dwell_pred_ok / g.dwell_pred
    s["M7_nearside_share_of_preds_pct"] = 100 * g.dwell_pred_near / g.dwell_pred
    s["M7_nearside_share_of_fp_pct"] = (
        100 * g.dwell_fp_near / (g.dwell_pred - g.dwell_pred_ok)
    )
    # RMS errors as secondary diagnostics
    s["pos_rmse_m"] = np.sqrt(g.pos_sq_sum / g.n_sec)
    s["spd_rmse_mph"] = np.sqrt(g.spd_sq_sum / g.n_sec)
    s["pos_mae_m"] = g.pos_abs_sum / g.n_sec
    s["spd_mae_mph"] = g.spd_abs_sum / g.n_sec
    return s.reset_index()
