"""Orchestrator: tune on the tuning half, evaluate on the held-out half.

Stages (run from scripts/frequency_analysis/):

    uv run python run_analysis.py --stage split   # report the trip split
    uv run python run_analysis.py --stage tune    # grid-search params (tuning half)
    uv run python run_analysis.py --stage eval    # final scoring (eval half)

Outputs in ``outputs/frequency_analysis/results/``:
    split.json             tuning / eval trip keys
    tuning_scores.csv      per (trip, freq, method, params) tuning-half scores
    tuned_params.json      argmin params per (metric, method, freq)
    per_trip_results.csv   eval-half scores with tuned params
    summary.csv            pooled curves (mean per-trip RMSE, door %s)
"""

from __future__ import annotations

import argparse
import itertools
import json
from multiprocessing import Pool

import numpy as np
import pandas as pd

import config as C
import methods as M
from pipeline import TripEval

# ------------------------------------------------------------- split

def split_trips(meta: list[dict]) -> dict:
    rng = np.random.default_rng(C.TUNE_SEED)
    tune, evaluate = [], []
    df = pd.DataFrame(meta)
    for _, g in df.groupby(["veh_id", "route_id"]):
        keys = list(g["trip_key"])
        rng.shuffle(keys)
        n_tune = int(round(len(keys) * C.TUNE_FRACTION))
        tune += keys[:n_tune]
        evaluate += keys[n_tune:]
    return {"tune": sorted(tune), "eval": sorted(evaluate)}


# ------------------------------------------------------------- param grids

def tuning_grid(method: str) -> list[dict]:
    if method == "PCHIP-VCHIP":
        return [{"alpha": a} for a in C.ALPHA_GRID]
    if method == "LOCREG-PCHIP":
        return [{"k": k} for k in C.K_GRID]
    if method == "LOCREG-PCHIP-V":
        return [{"kx": kx, "kv": kv}
                for kx, kv in itertools.product(C.KXV_GRID, C.KXV_GRID)]
    if method == "V-SPLINE-ME":
        return [{"gamma": g, "eta": e}
                for g, e in itertools.product(C.VSPLINE_GAMMA_GRID,
                                              C.VSPLINE_ETA_GRID)]
    return [{}]


# ------------------------------------------------------------- workers

_PINGS = None
_DOORS = None

def _init_worker():
    global _PINGS, _DOORS
    _PINGS = pd.read_parquet(C.CACHE_DIR / "trip_pings.parquet")
    _DOORS = pd.read_parquet(C.CACHE_DIR / "trip_doors.parquet")


_SIGS = None

def _trip_eval(trip_key: str) -> TripEval | None:
    global _SIGS
    if _SIGS is None:
        _SIGS = pd.read_parquet(C.CACHE_DIR / "trip_signals.parquet")
    p = _PINGS[_PINGS["trip_key"] == trip_key].reset_index(drop=True)
    d = _DOORS[_DOORS["trip_key"] == trip_key].reset_index(drop=True)
    s = _SIGS.loc[_SIGS["trip_key"] == trip_key, "x_sig_m"].to_numpy()
    te = TripEval(p, d, s, trip_key=trip_key)
    return te if te.usable() else None


def _tune_one(trip_key: str) -> list[dict]:
    """Tuning target = the M1/M2 holdout scores against measured pings."""
    te = _trip_eval(trip_key)
    if te is None:
        return []
    rows = []
    for freq in C.FREQS_S:
        for method in C.METHODS:
            if not M.TUNABLE[method]:
                continue
            for params in tuning_grid(method):
                s = te.score_holdout(freq, method, params)
                if not s.get("ho_n"):
                    continue
                rows.append({"trip_key": trip_key, "freq": freq,
                             "method": method, "params": json.dumps(params),
                             "ho_mae_x": s["ho_mae_x"],
                             "ho_mae_v": s["ho_mae_v"]})
    return rows


_TUNED = None

def _init_eval_worker(tuned):
    _init_worker()
    global _TUNED
    _TUNED = tuned


def _params_for(method: str, freq: int, metric: str) -> dict:
    """Tuned params if a tuning run exists and the method is tunable; else {}."""
    if _TUNED is None or not M.TUNABLE[method]:
        return {}
    return _TUNED[metric][method][str(freq)]


_R2P = None

def _eval_one(trip_key: str) -> list[dict]:
    global _R2P
    te = _trip_eval(trip_key)
    if te is None:
        return []
    rows = []
    for freq in C.FREQS_S:
        for method in C.METHODS:
            p2 = _params_for(method, freq, "M2")   # speed-tuned: M2..M7
            p1 = _params_for(method, freq, "M1")   # position-tuned: M1
            s = te.score(freq, method, p2)
            s.update(te.score_holdout(freq, method, p2))
            if p1 != p2:
                h1 = te.score_holdout(freq, method, p1)
                s["ho_mae_x"] = h1.get("ho_mae_x", np.nan)
                s["ho_rmse_x"] = h1.get("ho_rmse_x", np.nan)
            rows.append({"trip_key": trip_key, "freq": freq, "method": method,
                         "params_m1": json.dumps(p1),
                         "params_m2": json.dumps(p2), **s})
    # the real R2 feed (position-only -> PCHIP) as a reference point
    if _R2P is None:
        _R2P = pd.read_parquet(C.CACHE_DIR / "r2_trip_pings.parquet")
    g = _R2P[_R2P["trip_key"] == trip_key]
    if len(g) >= 5:
        from pipeline import epoch_s
        t = epoch_s(g["ping_dt"])
        s = te.score_arrays(t, g["x_m"].to_numpy(dtype=float), None,
                            C.R2_METHOD, {})
        rows.append({"trip_key": trip_key,
                     "freq": float(np.median(np.diff(t))),
                     "method": "R2", "params": "{}", **s})
    return rows


# ------------------------------------------------------------- stages

def _pool_map(fn, keys, init, initargs=()):
    with Pool(processes=max(2, __import__("os").cpu_count() - 2),
              initializer=init, initargs=initargs) as pool:
        out = []
        for i, rows in enumerate(pool.imap_unordered(fn, keys, chunksize=4)):
            out += rows
            if (i + 1) % 50 == 0:
                print(f"  {i + 1}/{len(keys)} trips")
        return out


def stage_split():
    meta = json.loads((C.CACHE_DIR / "trips.json").read_text())
    split = split_trips(meta)
    C.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (C.RESULTS_DIR / "split.json").write_text(json.dumps(split, indent=1))
    df = pd.DataFrame(meta).set_index("trip_key")
    for name in ("tune", "eval"):
        sub = df.loc[split[name]]
        print(f"{name}: {len(sub)} trips | per vehicle "
              f"{sub.groupby('veh_id').size().to_dict()} | "
              f"{sub['route_id'].nunique()} routes")


def stage_tune():
    split = json.loads((C.RESULTS_DIR / "split.json").read_text())
    rows = _pool_map(_tune_one, split["tune"], _init_worker)
    df = pd.DataFrame(rows)
    df.to_csv(C.RESULTS_DIR / "tuning_scores.csv", index=False)

    # k-fold CV over training trips: check the argmin is fold-stable
    keys = sorted(df["trip_key"].unique())
    fold_of = {k: i % C.N_FOLDS for i, k in enumerate(keys)}
    df["fold"] = df["trip_key"].map(fold_of)

    tuned = {"M1": {}, "M2": {}}
    report = []
    agg = df.groupby(["method", "freq", "params"])[
        ["ho_mae_x", "ho_mae_v"]].mean().reset_index()
    fold_agg = df.groupby(["method", "freq", "params", "fold"])[
        ["ho_mae_x", "ho_mae_v"]].mean().reset_index()
    for metric, col in (("M1", "ho_mae_x"), ("M2", "ho_mae_v")):
        for method in C.METHODS:
            tuned[metric][method] = {}
            for freq in C.FREQS_S:
                sub = agg[(agg["method"] == method) & (agg["freq"] == freq)]
                if not len(sub):
                    tuned[metric][method][str(freq)] = {}
                    continue
                best = sub.loc[sub[col].idxmin(), "params"]
                tuned[metric][method][str(freq)] = json.loads(best)
                fsub = fold_agg[(fold_agg["method"] == method)
                                & (fold_agg["freq"] == freq)]
                fold_best = [
                    fsub[fsub["fold"] == f].sort_values(col)["params"].iloc[0]
                    for f in range(C.N_FOLDS)
                    if len(fsub[fsub["fold"] == f])]
                report.append({
                    "metric": metric, "method": method, "freq": freq,
                    "chosen": best,
                    "fold_agreement": fold_best.count(best) / len(fold_best),
                    "fold_choices": fold_best})
    (C.RESULTS_DIR / "tuned_params.json").write_text(json.dumps(tuned, indent=1))
    pd.DataFrame(report).to_csv(C.RESULTS_DIR / "tuning_cv_report.csv", index=False)
    stable = pd.DataFrame(report)["fold_agreement"]
    print(f"tuned params -> tuned_params.json | CV fold agreement: "
          f"median {stable.median():.0%}, min {stable.min():.0%} "
          f"(details in tuning_cv_report.csv)")


def stage_eval():
    # With tunable methods active, report every model on the held-back test
    # subset; with none active, score ALL complete trips.
    has_tunables = any(M.TUNABLE[m] for m in C.METHODS)
    if has_tunables:
        split = json.loads((C.RESULTS_DIR / "split.json").read_text())
        keys = split["eval"]
        tuned = json.loads((C.RESULTS_DIR / "tuned_params.json").read_text())
    else:
        meta = json.loads((C.CACHE_DIR / "trips.json").read_text())
        keys = sorted(m["trip_key"] for m in meta)
        tuned = None
    rows = _pool_map(_eval_one, keys, _init_eval_worker, (tuned,))
    df = pd.DataFrame(rows)
    df.to_csv(C.RESULTS_DIR / "per_trip_results.csv", index=False)
    stage_summarize()


def stage_summarize():
    """Pool per_trip_results.csv into summary.csv (re-runnable on its own)."""
    df = pd.read_csv(C.RESULTS_DIR / "per_trip_results.csv")

    # pooled summary per (freq, method); the R2 reference pools across its
    # per-trip cadences and lands at their median
    groups = [((freq, method), g) for (freq, method), g
              in df[df["method"] != "R2"].groupby(["freq", "method"])]
    r2 = df[df["method"] == "R2"]
    if len(r2):
        groups.append(((float(r2["freq"].median()), "R2"), r2))
    recs = []
    for (freq, method), g in groups:
        r = {"freq": freq, "method": method, "n_trips": len(g),
             "rmse_x_mean": g["rmse_x"].mean(), "rmse_x_median": g["rmse_x"].median(),
             "rmse_x_q25": g["rmse_x"].quantile(.25), "rmse_x_q75": g["rmse_x"].quantile(.75),
             "rmse_v_mean": g["rmse_v"].mean(), "rmse_v_median": g["rmse_v"].median(),
             "rmse_v_q25": g["rmse_v"].quantile(.25), "rmse_v_q75": g["rmse_v"].quantile(.75),
             "mae_x_mean": g["mae_x"].mean(), "mae_v_mean": g["mae_v"].mean(),
             "door_total_s": g["door_total_s"].sum()}
        # M1/M2 holdout scoring vs measured pings (absent for the R2 rows)
        for c in ("ho_mae_x", "ho_rmse_x", "ho_mae_v", "ho_rmse_v"):
            r[f"{c}_mean"] = g[c].mean() if c in g else np.nan
        r["ho_n"] = int(g["ho_n"].sum()) if "ho_n" in g else 0
        # M3 doors-open stopped % per threshold (micro-pooled)
        tot = g["door_total_s"].sum()
        for th in (C.STOP_FTPS_MAIN, *C.STOP_FTPS_WHISKERS):
            r[f"door_stop_pct_{th}"] = (
                100.0 * g[f"door_stop_{th}"].sum() / tot if tot else np.nan)
        # M3b door location error. Pooled mean is outlier-dominated (rare
        # mis-snapped AVL door points on looping shapes reach km scale), so
        # the plotted statistic is the median across per-trip mean errors.
        r["doorloc_n"] = int(g["doorloc_n"].sum())
        r["doorloc_mae_m"] = (g["doorloc_abs_err_sum"].sum() / r["doorloc_n"]
                              if r["doorloc_n"] else np.nan)
        per_trip = (g["doorloc_abs_err_sum"]
                    / g["doorloc_n"].replace(0, np.nan)).dropna()
        r["doorloc_med_m"] = per_trip.median() if len(per_trip) else np.nan
        # M4 acceleration within bounds (micro-pooled %)
        na = g["n_accel"].sum()
        r["accel_tight_pct"] = 100.0 * g["accel_ok_tight"].sum() / na
        r["accel_loose_pct"] = 100.0 * g["accel_ok_loose"].sum() / na
        # M5 signal-zone travel time: weighted MAPE (%) + mean abs err (s)
        r["zone_n"] = int(g["zone_n"].sum())
        r["zone_wmape_pct"] = (100.0 * g["zone_abs_err_sum"].sum()
                               / g["zone_tt_base_sum"].sum())
        r["zone_mae_s"] = g["zone_abs_err_sum"].sum() / r["zone_n"]
        # M6 slow-state F1 (micro-pooled)
        tp, fp, fn = (g["slow_tp"].sum(), g["slow_fp"].sum(), g["slow_fn"].sum())
        r["slow_f1"] = 2 * tp / (2 * tp + fp + fn) if tp else 0.0
        # M7 event F1 (micro-pooled)
        nb, nr, nm = g["ev_base"].sum(), g["ev_rec"].sum(), g["ev_match"].sum()
        r["ev_f1"] = 2 * nm / (nb + nr) if (nb + nr) else np.nan
        r["ev_base"], r["ev_rec"], r["ev_match"] = int(nb), int(nr), int(nm)
        recs.append(r)
    pd.DataFrame(recs).to_csv(C.RESULTS_DIR / "summary.csv", index=False)
    print("summary ->", C.RESULTS_DIR / "summary.csv")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=["split", "tune", "eval", "summarize"],
                    required=True)
    args = ap.parse_args()
    {"split": stage_split, "tune": stage_tune, "eval": stage_eval,
     "summarize": stage_summarize}[args.stage]()
