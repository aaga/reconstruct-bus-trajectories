"""Score the real low-frequency AVL feed as a reference point.

For every cached trip with AVL coverage: reconstruct from the AVL pings
(position + integer-mph speed) with PCHIP (position-only) and VCHIP-ME
(velocity-aware), then score

  M1/M2  at the same held-out VTRAK pings the ladder curves use
  M3/M5  on the common 1 s grid (doors-open %, signal-zone travel time)

Writes results/avl_reference.csv (per-trip) and prints the pooled row that
plot_results.py overlays on the figures.

    uv run python avl_eval.py
"""

from __future__ import annotations

import json
import zlib

import numpy as np
import pandas as pd

import config as C
import methods as M
from pipeline import epoch_s
from run_analysis import _init_worker, _trip_eval

AVL_METHODS = ("PCHIP", "VCHIP-ME")


def self_holdout(trip_key, t, x, v, method):
    """Paper-literal 5% holdout on the AVL feed's OWN pings: reconstruct
    from the remaining 95%, score at the held-out pings against the
    measured AVL position/speed."""
    rng = np.random.default_rng(
        (C.HOLDOUT_SEED, zlib.crc32(trip_key.encode())))
    held = np.zeros(len(t), dtype=bool)
    n_hold = max(1, int(round(len(t) * C.HOLDOUT_FRACTION)))
    held[rng.choice(len(t), n_hold, replace=False)] = True
    tk, xk, vk = t[~held], x[~held], v[~held]
    inside = held & (t >= tk[0]) & (t <= tk[-1])
    if inside.sum() < 1 or (~held).sum() < 4:
        return {"ho_n": 0}
    H = M.BUILDERS[method](tk, xk, vk)
    ex = H.pos(t[inside]) - x[inside]
    ev = H.vel(t[inside]) - v[inside]
    return {"ho_n": int(inside.sum()),
            "ho_mae_x": float(np.mean(np.abs(ex))),
            "ho_rmse_x": float(np.sqrt(np.mean(ex**2))),
            "ho_mae_v": float(np.mean(np.abs(ev))),
            "ho_rmse_v": float(np.sqrt(np.mean(ev**2)))}


def main() -> None:
    _init_worker()
    meta = json.loads((C.CACHE_DIR / "trips.json").read_text())
    avl = pd.read_parquet(C.CACHE_DIR / "avl_trip_pings.parquet")
    rows = []
    for m in meta:
        k = m["trip_key"]
        g = avl[avl["trip_key"] == k]
        if len(g) < 5:
            continue
        te = _trip_eval(k)
        if te is None:
            continue
        t = epoch_s(g["ping_dt"])
        x = g["x_m"].to_numpy(float)
        v = np.nan_to_num(g["v_mps"].to_numpy(float), nan=0.0)
        cadence = float(np.median(np.diff(t)))
        max_gap = float(np.max(np.diff(t)))
        for method in AVL_METHODS:
            s = te.score_arrays(t, x, v, method, {})       # M3/M5 (grid)
            # M1/M2: self-holdout on the AVL feed's own pings (2026-08-20);
            # the VTRAK-referenced variant is kept alongside as vt_*
            vt = te.score_holdout_arrays(t, x, v, method, {})
            s.update({f"vt_{key}": val for key, val in vt.items()})
            s.update(self_holdout(k, t, x, v, method))
            rows.append({"trip_key": k, "cadence_s": cadence,
                         "max_gap_s": max_gap, "method": method, **s})
    df = pd.DataFrame(rows)
    df.to_csv(C.RESULTS_DIR / "avl_reference.csv", index=False)

    for label, sub in (("all trips", df),
                       ("covered (max gap <= 120s)",
                        df[df["max_gap_s"] <= 120.0])):
        print(f"AVL reference [{label}]: {sub.trip_key.nunique()} trips, "
              f"median cadence {sub.cadence_s.median():.1f}s")
        for method, g in sub.groupby("method"):
            tot = g["door_total_s"].sum()
            print(f"  {method:9s} M1={g.ho_mae_x.mean():6.2f} m  "
                  f"M2={g.ho_mae_v.mean() * C.MPS_TO_FTPS:5.2f} ft/s  "
                  f"M3={100 * g[f'door_stop_{C.STOP_FTPS_MAIN}'].sum() / tot:5.1f}%  "
                  f"M5={100 * g.zone_abs_err_sum.sum() / g.zone_tt_base_sum.sum():5.1f}%")


if __name__ == "__main__":
    main()
