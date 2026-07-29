"""Welford merge, histogram quantiles, and metric derivation."""

from __future__ import annotations

import numpy as np
import pytest

from analysis.network.stats import (
    HIST_EDGES,
    N_BUCKETS,
    derive_metrics,
    hist_counts,
    hist_index,
    mean_std,
    quantile_from_hist,
    welford_from_samples,
    welford_merge,
)


def test_hist_index_boundaries():
    assert hist_index(np.array([0.1]))[0] == 0  # underflow
    assert hist_index(np.array([100.0]))[0] == N_BUCKETS - 1  # overflow
    # A value exactly on the first edge goes to bucket 1 (side="right").
    assert hist_index(np.array([HIST_EDGES[0]]))[0] == 1


def test_hist_counts_total():
    r = np.array([0.5, 1.0, 1.1, 2.0, 7.0])
    assert hist_counts(r).sum() == len(r)


def test_welford_merge_equals_whole():
    rng = np.random.default_rng(7)
    x = rng.normal(30.0, 12.0, 1000)
    whole = welford_from_samples(x)
    for split in (1, 137, 500, 999):
        merged = welford_merge(*welford_from_samples(x[:split]), *welford_from_samples(x[split:]))
        assert merged[0] == whole[0]
        np.testing.assert_allclose(merged[1], whole[1], rtol=1e-12)
        np.testing.assert_allclose(merged[2], whole[2], rtol=1e-9)


def test_welford_merge_with_empty():
    a = welford_from_samples(np.array([1.0, 2.0, 3.0]))
    assert welford_merge(*a, *welford_from_samples(np.array([]))) == a
    assert welford_merge(*welford_from_samples(np.array([])), *a) == a


def test_mean_std_matches_numpy():
    x = np.array([4.0, 8.0, 15.0, 16.0, 23.0, 42.0])
    n, s, m2 = welford_from_samples(x)
    mean, std = mean_std(n, s, m2)
    np.testing.assert_allclose(mean, x.mean())
    np.testing.assert_allclose(std, x.std())  # population std (ddof=0)


def test_quantile_from_hist_accuracy():
    # Against exact quantiles of a big lognormal sample: within ~5% of value.
    rng = np.random.default_rng(3)
    ratios = rng.lognormal(0.2, 0.4, 50_000)
    hist = hist_counts(ratios)
    for q in (0.5, 0.9):
        approx = quantile_from_hist(hist, q)
        exact = float(np.quantile(ratios, q))
        assert abs(approx - exact) / exact < 0.05, (q, approx, exact)


def test_quantile_from_hist_empty():
    assert np.isnan(quantile_from_hist(np.zeros(N_BUCKETS), 0.5))


def test_derive_metrics_signs_and_scaling():
    # All traversals exactly at free flow -> ratio 1.0 -> ~zero delays.
    ratios = np.full(200, 1.0)
    delays = (ratios - 1.0) * 60.0
    n, s, m2 = welford_from_samples(delays)
    m = derive_metrics(n, s, m2, hist_counts(ratios), 60.0)
    assert m["n"] == 200
    assert abs(m["mean_delay_s"]) < 1e-9
    # Ratio-1.0 sits inside a bucket; hist-derived medians carry in-bucket
    # interpolation error bounded by the bucket width (~10% of value here).
    assert abs(m["median_delay_s"]) < 6.0
    assert m["buffer_s"] >= 0.0


def test_derive_metrics_congested_segment():
    rng = np.random.default_rng(11)
    ratios = rng.lognormal(0.6, 0.3, 500)  # heavily delayed
    t_ff = 45.0
    delays = (ratios - 1.0) * t_ff
    n, s, m2 = welford_from_samples(delays)
    m = derive_metrics(n, s, m2, hist_counts(ratios), t_ff)
    assert m["mean_delay_s"] > 20.0
    assert m["p90_delay_s"] > m["median_delay_s"]
    assert m["buffer_s"] > 0.0


def test_derive_stat_families_consistent():
    import numpy as np
    from analysis.network.stats import (
        DWELL_EDGES, PAX_EDGES, derive_stat, hist_counts, welford_from_samples,
    )
    rng = np.random.default_rng(5)
    n, t_ff = 800, 50.0
    ratios = rng.lognormal(0.3, 0.4, n)
    delays = (ratios - 1) * t_ff
    n_door = 500
    # Event-classified semantics: dwell = door∪event union seconds; nd =
    # zero-door-overlap event seconds (0-centric, nonnegative); pax ≥ 0.
    dwell = np.abs(rng.normal(9, 5, n_door))
    nd = np.where(rng.random(n_door) < 0.5, 0.0, rng.lognormal(2.6, 0.8, n_door))
    loads = rng.integers(1, 50, n_door).astype(float)
    pax = nd * loads
    acc = {
        "n": n, "sum": float(delays.sum()), "m2": welford_from_samples(delays)[2],
        "hist": hist_counts(ratios),
        "n_door": n_door,
        "sum_dwell": float(dwell.sum()),
        "sum_delay_door": float(delays[:n_door].sum()),
        "sum_nd": float(nd.sum()),
        "m2_dw": welford_from_samples(dwell)[2],
        "m2_nd": welford_from_samples(nd)[2],
        "hist_dw": hist_counts(dwell / t_ff, DWELL_EDGES),
        "hist_nd": hist_counts(nd / t_ff, DWELL_EDGES),
        "sum_pax": float(pax.sum()),
        "m2_pax": welford_from_samples(pax)[2],
        "hist_pax": hist_counts(pax, PAX_EDGES),
    }
    # means exact
    assert derive_stat("overall", "mean", acc, t_ff) == pytest.approx(delays.mean())
    assert derive_stat("dwell", "mean", acc, t_ff) == pytest.approx(dwell.mean())
    assert derive_stat("nondwell", "mean", acc, t_ff) == pytest.approx(nd.mean())
    assert derive_stat("pax", "mean", acc, t_ff) == pytest.approx(pax.mean())
    # stds exact
    assert derive_stat("pax", "std", acc, t_ff) == pytest.approx(pax.std())
    # hist quantiles within tolerance of exact
    for fam, samples in (("overall", delays), ("dwell", dwell), ("nondwell", nd)):
        for st, q in (("median", 0.5), ("p95", 0.95)):
            got = derive_stat(fam, st, acc, t_ff)
            exact = float(np.quantile(samples, q))
            assert abs(got - exact) <= max(2.5, 0.12 * abs(exact)), (fam, st, got, exact)
    # buffer = p95 - mean by construction
    assert derive_stat("overall", "buffer", acc, t_ff) == pytest.approx(
        derive_stat("overall", "p95", acc, t_ff) - derive_stat("overall", "mean", acc, t_ff))
    # empty door subset -> NaN
    acc0 = dict(acc, n_door=0)
    import math
    assert math.isnan(derive_stat("dwell", "mean", acc0, t_ff))
