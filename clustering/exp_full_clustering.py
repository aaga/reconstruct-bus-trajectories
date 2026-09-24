"""Experiment A: cluster FULL trip trajectories, compare methods.

Methods (see clustering/MEMO.md for rationale + citations):
  M1  runtime-only k-means           (1-D strawman baseline)
  M2  scalar-feature k-means         (interpretable engineered features)
  M3  Ward on deviation profiles     (space-aligned Euclidean; the "default")
  M4  FPCA scores -> GMM             (functional PCA; BIC-selected k)
  M5  DTW k-medoids on d(t)          (time-aligned elastic; robustness check)
  M6  k-Shape on pace residuals      (shape-of-delay only, amplitude removed)

Evaluation:
  - silhouette over k=2..8 in each method's own space (where defined)
  - cross-method agreement (adjusted Rand index) at the working k
  - external validation NOT used for fitting: time-of-day / weekday
    composition per cluster, runtime distributions
  - figures: cluster mean deviation curves, FPCA loadings, hour heatmap

Run:  PYTHONPATH=src:clustering uv run python clustering/exp_full_clustering.py
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

import config as C  # noqa: E402
import representations as R  # noqa: E402

from sklearn.cluster import AgglomerativeClustering, KMeans  # noqa: E402
from sklearn.decomposition import PCA  # noqa: E402
from sklearn.metrics import adjusted_rand_score, silhouette_score  # noqa: E402
from sklearn.mixture import GaussianMixture  # noqa: E402
from sklearn.preprocessing import StandardScaler  # noqa: E402

SEED = 0
K_RANGE = range(2, 9)
K_WORK = 4  # working k for cross-method comparison (see silhouette scan)


# ---------------------------------------------------------------- methods
def m1_runtime(T, k):
    x = T[:, [-1]]
    return KMeans(k, n_init=10, random_state=SEED).fit_predict(x), x


def m2_features(T, d_grid, k):
    X = StandardScaler().fit_transform(R.scalar_features(T, d_grid))
    return KMeans(k, n_init=10, random_state=SEED).fit_predict(X), X


def m3_ward_dev(T, k):
    X = R.deviation_profiles(T)
    return AgglomerativeClustering(k, linkage="ward").fit_predict(X), X


def m4_fpca_gmm(T, k, n_pc=4):
    X = R.deviation_profiles(T)
    pca = PCA(n_components=n_pc, random_state=SEED).fit(X)
    S = pca.transform(X)
    gmm = GaussianMixture(k, covariance_type="full", random_state=SEED,
                          n_init=5).fit(S)
    return gmm.predict(S), S, pca, gmm


def dtw_distance_matrix(T):
    from tslearn.metrics import cdist_dtw
    series = R.time_view(T, dt_s=30.0)
    from tslearn.utils import to_time_series_dataset
    ds = to_time_series_dataset(series)  # NaN-padded; tslearn handles lengths
    return cdist_dtw(ds, n_jobs=-1)


def m5_dtw_medoids(D, k, n_iter=50):
    """PAM-style k-medoids on the precomputed DTW matrix.

    (Average-linkage agglomerative on DTW chains into one giant cluster plus
    singletons here, so a proper alternating k-medoids is used instead.)
    """
    rng = np.random.default_rng(SEED)
    medoids = rng.choice(len(D), k, replace=False)
    for _ in range(n_iter):
        lab = np.argmin(D[:, medoids], axis=1)
        new = np.array([np.where(lab == c)[0][
            np.argmin(D[np.ix_(lab == c, lab == c)].sum(axis=1))]
            if (lab == c).any() else medoids[c] for c in range(k)])
        if (new == medoids).all():
            break
        medoids = new
    return np.argmin(D[:, medoids], axis=1), list(medoids)


def m6_kshape(T, d_grid, k):
    """k-Shape on z-normalized pace residuals (amplitude deliberately removed)."""
    from tslearn.clustering import KShape
    from tslearn.preprocessing import TimeSeriesScalerMeanVariance
    pace = R.pace_profiles(T, d_grid)
    resid = pace - np.median(pace, axis=0, keepdims=True)
    X = TimeSeriesScalerMeanVariance().fit_transform(resid[:, :, None])
    return KShape(n_clusters=k, random_state=SEED).fit_predict(X)


# ---------------------------------------------------------------- evaluation
def silhouette_scan(name, X, algo, metric="euclidean", D=None):
    rows = []
    for k in K_RANGE:
        if D is not None:
            lab, _ = m5_dtw_medoids(D, k)
            s = silhouette_score(D, lab, metric="precomputed")
        else:
            lab = algo(k)
            s = silhouette_score(X, lab, metric=metric)
        rows.append({"method": name, "k": k, "silhouette": s})
    return rows


def hour_mix(meta, lab):
    df = meta.assign(cluster=lab)
    return pd.crosstab(df.cluster, pd.cut(
        df.hour, [0, 6.5, 9.5, 15, 18.5, 24],
        labels=["night/early", "AM peak", "midday", "PM peak", "evening"]),
        normalize="index").round(2)


def main():
    d_grid, T, meta = R.load_profiles()
    N = len(T)
    print(f"{N} trips, {len(d_grid)} grid pts")
    C.FIG_DIR.mkdir(parents=True, exist_ok=True)

    # ---- silhouette scans in each method's own space
    scan = []
    scan += silhouette_scan("M1_runtime", T[:, [-1]],
                            lambda k: m1_runtime(T, k)[0])
    Xf = StandardScaler().fit_transform(R.scalar_features(T, d_grid))
    scan += silhouette_scan("M2_features", Xf,
                            lambda k: KMeans(k, n_init=10, random_state=SEED)
                            .fit_predict(Xf))
    Xd = R.deviation_profiles(T)
    scan += silhouette_scan("M3_ward_dev", Xd,
                            lambda k: AgglomerativeClustering(k, linkage="ward")
                            .fit_predict(Xd))
    S4 = PCA(4, random_state=SEED).fit_transform(Xd)
    scan += silhouette_scan("M4_fpca", S4,
                            lambda k: GaussianMixture(k, random_state=SEED,
                                                      n_init=5)
                            .fit(S4).predict(S4))
    print("computing DTW distance matrix ...")
    D = dtw_distance_matrix(T)
    scan += silhouette_scan("M5_dtw", None, None, D=D)
    scan_df = pd.DataFrame(scan)
    scan_df.to_csv(C.OUT_DIR / "silhouette_scan.csv", index=False)
    print(scan_df.pivot(index="k", columns="method",
                        values="silhouette").round(3))

    # ---- BIC for the GMM (FPCA) view
    bic = {k: GaussianMixture(k, covariance_type="full", random_state=SEED,
                              n_init=5).fit(S4).bic(S4) for k in K_RANGE}
    print("FPCA-GMM BIC by k:", {k: round(v) for k, v in bic.items()})

    # ---- fit all methods at the working k
    k = K_WORK
    labs = {}
    labs["M1_runtime"], _ = m1_runtime(T, k)
    labs["M2_features"], _ = m2_features(T, d_grid, k)
    labs["M3_ward_dev"], _ = m3_ward_dev(T, k)
    labs["M4_fpca"], S, pca, gmm = m4_fpca_gmm(T, k)
    labs["M5_dtw"], medoids = m5_dtw_medoids(D, k)
    labs["M6_kshape"] = m6_kshape(T, d_grid, k)

    # ---- cross-method agreement
    names = list(labs)
    ari = pd.DataFrame(
        [[adjusted_rand_score(labs[a], labs[b]) for b in names] for a in names],
        index=names, columns=names).round(2)
    ari.to_csv(C.OUT_DIR / "ari_cross_method.csv")
    print("\nAdjusted Rand agreement:\n", ari)

    # ---- per-method external validation + summary
    summaries = []
    for name, lab in labs.items():
        df = meta.assign(cluster=lab, runtime_min=T[:, -1] / 60)
        g = df.groupby("cluster").agg(
            n=("cluster", "size"),
            runtime_med=("runtime_min", "median"),
            runtime_iqr=("runtime_min",
                         lambda s: s.quantile(.75) - s.quantile(.25)),
            weekend_frac=("weekday",
                          lambda s: s.isin(["Saturday", "Sunday"]).mean()),
        ).round(1)
        g["method"] = name
        summaries.append(g.reset_index())
        print(f"\n== {name} ==\n{g}\n{hour_mix(meta, lab)}")
    pd.concat(summaries).to_csv(C.OUT_DIR / "cluster_summaries.csv", index=False)

    # ---- figures -------------------------------------------------------
    med = np.median(T, axis=0)
    km = d_grid / 1000

    # (1) cluster mean deviation curves for M3, M4, M5
    fig, axes = plt.subplots(1, 3, figsize=(16, 5), sharey=True)
    for ax, name in zip(axes, ["M3_ward_dev", "M4_fpca", "M5_dtw"]):
        lab = labs[name]
        for c in range(k):
            sel = lab == c
            dev = (T[sel] - med).mean(axis=0) / 60
            ax.plot(km, dev, lw=2, label=f"c{c} (n={sel.sum()})")
        ax.axhline(0, color="k", lw=.5)
        ax.set_title(name)
        ax.set_xlabel("km along route")
        ax.legend(fontsize=8)
    axes[0].set_ylabel("mean deviation from median trip (min)")
    fig.suptitle(f"Cluster mean cumulative-deviation curves (k={k})")
    fig.savefig(C.FIG_DIR / "cluster_mean_deviations.png", dpi=110,
                bbox_inches="tight")

    # (2) FPCA loadings + explained variance
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(13, 4.5))
    for i in range(pca.n_components_):
        a1.plot(km, pca.components_[i],
                label=f"PC{i+1} ({pca.explained_variance_ratio_[i]:.0%})")
    a1.axhline(0, color="k", lw=.5)
    a1.set_xlabel("km along route"); a1.set_title("FPCA loadings on t(d) deviations")
    a1.legend()
    sc = a2.scatter(S[:, 0], S[:, 1], c=meta.hour, cmap="twilight_shifted",
                    s=12)
    a2.set_xlabel("PC1 score"); a2.set_ylabel("PC2 score")
    a2.set_title("trips in FPCA space, colored by local hour")
    fig.colorbar(sc, ax=a2, label="hour")
    fig.savefig(C.FIG_DIR / "fpca.png", dpi=110, bbox_inches="tight")

    # (3) all trips colored by M4 cluster (small multiples)
    lab = labs["M4_fpca"]
    fig, axes = plt.subplots(1, k, figsize=(4 * k, 4.5), sharey=True)
    for c, ax in enumerate(axes):
        for i in np.where(lab == c)[0]:
            ax.plot(km, (T[i] - med) / 60, lw=.4, alpha=.3, color="steelblue")
        ax.plot(km, (T[lab == c] - med).mean(axis=0) / 60, color="crimson", lw=2)
        ax.axhline(0, color="k", lw=.5)
        ax.set_title(f"M4 cluster {c} (n={(lab == c).sum()})")
        ax.set_xlabel("km")
    axes[0].set_ylabel("deviation from median trip (min)")
    fig.savefig(C.FIG_DIR / "m4_cluster_members.png", dpi=110,
                bbox_inches="tight")

    # ---- persist labels for the partial-matching experiment
    out = meta.assign(**{n: l for n, l in labs.items()})
    out.to_csv(C.OUT_DIR / "labels_full.csv", index=False)
    np.save(C.OUT_DIR / "dtw_matrix.npy", D)
    print(f"\nwrote labels -> {C.OUT_DIR / 'labels_full.csv'}")


if __name__ == "__main__":
    main()
