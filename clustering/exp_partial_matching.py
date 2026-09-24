"""Experiment B: match a PARTIAL (in-progress) trip to history and predict
its remaining runtime.

Setup: leave-one-date-out over the 7 archive days. A test trip is observed
up to fraction q of the route distance (q in {0.25, 0.5, 0.75}); predict the
time still needed to reach the end of the grid. Predictors:

  P1  GLOBAL     median remaining over all train trips (unconditional)
  P2  TOD        median remaining over train trips departing within +/-90 min
                 of the test trip's local departure time (schedule-like)
  P3  KNN        k-nearest-neighbours on the space-aligned prefix t(d)
                 (Euclidean), median of donors' remaining times
                 [Sinn et al. 2012 style, hard-kNN variant]
  P4  CLUSTER    Cristobal-style: k-means clusters on train FPCA scores;
                 assign test prefix to nearest cluster-mean prefix; predict
                 that cluster's median remaining
  P5  BLP        Gaussian conditional expectation of remaining time given the
                 prefix, from the train sample covariance (what PACE computes
                 for functional data; equivalently a ridge linear readout)
  P6  OE-DTW     open-end DTW in the TIME domain: match the query's d(t)
                 series against full train d(t) series with a free endpoint;
                 donors vote with (their total time - matched elapsed time)
  P7  KNN+TOD    P3 restricted to donors departing within +/-90 min

Run:  PYTHONPATH=src:clustering uv run python clustering/exp_partial_matching.py
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

import config as C  # noqa: E402
import representations as R  # noqa: E402

from sklearn.cluster import KMeans  # noqa: E402
from sklearn.decomposition import PCA  # noqa: E402

SEED = 0
FRACTIONS = (0.25, 0.50, 0.75)
KNN_K = 15
N_PC = 4
K_CLUSTERS = 4
DT_S = 30.0  # time-domain sampling step for OE-DTW


# ---------------------------------------------------------------- predictors
def p3_knn(prefix_train: np.ndarray, remain_train: np.ndarray,
           prefix_test: np.ndarray, k: int = KNN_K) -> np.ndarray:
    d2 = ((prefix_train[None] - prefix_test[:, None]) ** 2).sum(-1)
    nn = np.argsort(d2, axis=1)[:, :k]
    return np.median(remain_train[nn], axis=1)


def p4_cluster(T_train, dev_train, remain_train, prefix_test, iq):
    pca = PCA(N_PC, random_state=SEED).fit(dev_train)
    lab = KMeans(K_CLUSTERS, n_init=10, random_state=SEED).fit_predict(
        pca.transform(dev_train))
    means = np.stack([T_train[lab == c].mean(0) for c in range(K_CLUSTERS)])
    med_remain = np.array([np.median(remain_train[lab == c])
                           for c in range(K_CLUSTERS)])
    d2 = ((means[None, :, :iq] - prefix_test[:, None]) ** 2).sum(-1)
    return med_remain[np.argmin(d2, axis=1)]


def p5_blp(T_train, T_test_prefix, iq, ridge=60.0 ** 2):
    """Gaussian conditional expectation (best linear predictor) of the
    remaining time given the observed prefix — what PACE computes for
    functional data, taken directly from the train sample covariance.

    ridge is an observation-noise variance (s^2) on each prefix point,
    which keeps the prefix covariance well-conditioned.
    """
    y = T_train[:, -1] - T_train[:, iq - 1]
    X = T_train[:, :iq]
    Xm, ym = X.mean(0), y.mean()
    Xc = X - Xm
    S_xx = Xc.T @ Xc / len(X) + ridge * np.eye(iq)
    s_xy = Xc.T @ (y - ym) / len(X)
    beta = np.linalg.solve(S_xx, s_xy)
    return ym + (T_test_prefix - Xm) @ beta


def p7_knn_tod(prefix_tr, remain_tr, hours_tr, prefix_te, hours_te,
               window_h=1.5, k=KNN_K):
    """kNN on the prefix, restricted to a time-of-day window (regime-
    conditioned trajectory matching)."""
    out = np.empty(len(prefix_te))
    for i in range(len(prefix_te)):
        dh = np.abs(hours_tr - hours_te[i])
        sel = np.where(np.minimum(dh, 24 - dh) <= window_h)[0]
        if len(sel) < k:
            sel = np.arange(len(prefix_tr))
        d2 = ((prefix_tr[sel] - prefix_te[i]) ** 2).sum(-1)
        out[i] = np.median(remain_tr[sel[np.argsort(d2)[:k]]])
    return out


def batched_oe_dtw_remaining(q_series, ref_mat, ref_len, ref_total_s,
                             k: int = KNN_K):
    """Open-end DTW of one query d(t) series against all refs at once.

    Asymmetric step (each query sample consumed once, ref index advances
    0/1/2), so the DP vectorizes across the (n_ref, L_ref) lattice per query
    step. Endpoint is free: best column j* of the final row gives the
    matched elapsed time j*·DT_S in each ref. Donors vote with their
    remaining time from that point; returns the median over the k best.
    """
    n_ref, L = ref_mat.shape
    INF = np.inf
    D = np.full((n_ref, L), INF)
    c0 = np.abs(ref_mat - q_series[0])
    D[:, 0] = c0[:, 0]
    D[:, 1] = c0[:, 1]  # open-begin not allowed beyond a 1-step slack
    for qi in q_series[1:]:
        c = np.abs(ref_mat - qi)
        prev = D
        D = np.minimum(prev, np.minimum(
            np.concatenate([np.full((n_ref, 1), INF), prev[:, :-1]], 1),
            np.concatenate([np.full((n_ref, 2), INF), prev[:, :-2]], 1)))
        D = D + c
    D[~np.isfinite(ref_mat)] = INF  # padding columns can't be endpoints
    j_star = np.nanargmin(np.where(np.isfinite(D), D, INF), axis=1)
    score = D[np.arange(n_ref), j_star] / len(q_series)
    donors = np.argsort(score)[:k]
    remain = ref_total_s[donors] - j_star[donors] * DT_S
    return np.median(np.maximum(remain, 0.0))


def time_domain_matrix(T):
    """d(t) series sampled every DT_S, NaN-padded into one matrix."""
    series = R.time_view(T, dt_s=DT_S)
    L = max(len(s) for s in series)
    M = np.full((len(series), L), np.nan)
    for i, s in enumerate(series):
        M[i, : len(s)] = s
    return M


# ---------------------------------------------------------------- experiment
def main():
    d_grid, T, meta = R.load_profiles()
    dates = meta.date.unique()
    K = len(d_grid)
    Mtime = time_domain_matrix(T)
    len_time = np.array([np.isfinite(Mtime[i]).sum() for i in range(len(T))])
    ref_pad = np.where(np.isfinite(Mtime), Mtime, np.inf)

    rows = []
    for q in FRACTIONS:
        iq = int(q * K)
        remain_all = T[:, -1] - T[:, iq - 1]
        for day in dates:
            te = np.where(meta.date == day)[0]
            tr = np.where(meta.date != day)[0]
            T_tr, T_te = T[tr], T[te]
            dev_tr = T_tr - np.median(T_tr, axis=0, keepdims=True)
            prefix_tr, prefix_te = T_tr[:, :iq], T_te[:, :iq]
            remain_tr, truth = remain_all[tr], remain_all[te]

            pred = {"P1_global": np.full(len(te), np.median(remain_tr))}

            tod = np.empty(len(te))
            for i, ti in enumerate(te):
                dh = np.abs(meta.hour.values[tr] - meta.hour.values[ti])
                dh = np.minimum(dh, 24 - dh)
                sel = dh <= 1.5
                tod[i] = np.median(remain_tr[sel]) if sel.sum() >= 5 \
                    else np.median(remain_tr)
            pred["P2_tod"] = tod

            pred["P3_knn"] = p3_knn(prefix_tr, remain_tr, prefix_te)
            pred["P4_cluster"] = p4_cluster(T_tr, dev_tr, remain_tr,
                                            prefix_te, iq)
            pred["P5_blp"] = p5_blp(T_tr, prefix_te, iq)
            pred["P7_knn_tod"] = p7_knn_tod(
                prefix_tr, remain_tr, meta.hour.values[tr],
                prefix_te, meta.hour.values[te])

            oe = np.empty(len(te))
            for i, ti in enumerate(te):
                n_obs = int(round(T[ti, iq - 1] / DT_S)) + 1
                qs = Mtime[ti, :n_obs]
                qs = qs[np.isfinite(qs)]
                oe[i] = batched_oe_dtw_remaining(
                    qs, ref_pad[tr], len_time[tr], T_tr[:, -1])
            # donor remaining above is measured to the donor's own end time;
            # convert: truth is time from d_q to grid end, donor vote already is.
            pred["P6_oedtw"] = oe

            for name, p in pred.items():
                err = (p - truth) / 60.0
                for e, ti in zip(err, te):
                    rows.append({"q": q, "date": day, "method": name,
                                 "trip": meta.trip_key.values[ti],
                                 "err_min": e,
                                 "true_remain_min": truth[list(te).index(ti)]
                                 / 60.0})

    res = pd.DataFrame(rows)
    res.to_csv(C.OUT_DIR / "partial_matching_errors.csv", index=False)

    summary = (res.assign(abs_err=res.err_min.abs())
               .groupby(["q", "method"])
               .agg(mae=("abs_err", "mean"),
                    rmse=("err_min", lambda e: np.sqrt((e ** 2).mean())),
                    p90=("abs_err", lambda e: e.quantile(.9)),
                    bias=("err_min", "mean"))
               .round(2))
    summary.to_csv(C.OUT_DIR / "partial_matching_summary.csv")
    print(summary.to_string())

    # MAE vs fraction figure
    fig, ax = plt.subplots(figsize=(8, 5))
    piv = summary.reset_index().pivot(index="q", columns="method",
                                      values="mae")
    for m in piv.columns:
        ax.plot(piv.index * 100, piv[m], "o-", label=m)
    ax.set_xlabel("% of route observed")
    ax.set_ylabel("MAE of remaining-runtime prediction (min)")
    ax.set_title("Partial-trajectory matching: leave-one-date-out")
    ax.legend()
    fig.savefig(C.FIG_DIR / "partial_matching_mae.png", dpi=110,
                bbox_inches="tight")
    print(f"wrote {C.FIG_DIR / 'partial_matching_mae.png'}")


if __name__ == "__main__":
    main()
