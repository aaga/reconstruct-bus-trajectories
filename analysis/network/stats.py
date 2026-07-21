"""Pure statistics for network segment aggregation.

Single source of truth for the binning/merge/quantile math used by BOTH the
offline payload builder (``build_payloads.py``) and the dashboard's JS decoder
(``dashboard/app/network_data.js`` mirrors these functions; golden vectors
emitted by ``emit_golden_vectors`` keep the two in lockstep).

Per (segment × filter-bin) we store exactly:
    n          traversal count
    sum_delay  Σ (t_obs − t_ff)            [seconds]
    m2         Σ (delay − mean)²           (Welford, for variance)
    hist[16]   counts of delay ratio t_obs/t_ff in fixed log-spaced buckets

Means/variances combine exactly across bins (parallel-axis Welford merge);
medians/p90s come from the summed histogram with in-bucket linear
interpolation (±~4% of value — fine for choropleths; the AOI engine uses
exact offline quantiles instead).
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

# Delay-ratio histogram: bucket 0 = underflow (< EDGES[0]), buckets 1..14
# between the 15 log-spaced edges, bucket 15 = overflow (>= EDGES[-1]).
N_BUCKETS = 16
RATIO_LO = 0.75
RATIO_HI = 6.0
HIST_EDGES = np.geomspace(RATIO_LO, RATIO_HI, N_BUCKETS - 1)  # 15 edges
# Virtual outer edges for in-bucket interpolation in the open-ended buckets.
UNDER_EDGE = 0.5
OVER_EDGE = 8.0


def hist_index(ratios: np.ndarray) -> np.ndarray:
    """Bucket index (0..15) for each delay ratio."""
    return np.searchsorted(HIST_EDGES, np.asarray(ratios, dtype=float), side="right")


def hist_counts(ratios: np.ndarray) -> np.ndarray:
    """16-bucket histogram of delay ratios."""
    return np.bincount(hist_index(ratios), minlength=N_BUCKETS).astype(np.int64)


def welford_from_samples(x: np.ndarray) -> tuple[int, float, float]:
    """(n, sum, m2) for a sample array."""
    x = np.asarray(x, dtype=float)
    n = len(x)
    if n == 0:
        return 0, 0.0, 0.0
    mean = float(x.mean())
    return n, float(x.sum()), float(((x - mean) ** 2).sum())


def welford_merge(
    n1: int, s1: float, m2_1: float, n2: int, s2: float, m2_2: float
) -> tuple[int, float, float]:
    """Parallel-axis merge of two (n, sum, m2) accumulators. Exact."""
    n = n1 + n2
    if n == 0:
        return 0, 0.0, 0.0
    if n1 == 0:
        return n2, s2, m2_2
    if n2 == 0:
        return n1, s1, m2_1
    delta = s2 / n2 - s1 / n1
    m2 = m2_1 + m2_2 + delta * delta * n1 * n2 / n
    return n, s1 + s2, m2


def mean_std(n: int, s: float, m2: float) -> tuple[float, float]:
    if n == 0:
        return float("nan"), float("nan")
    mean = s / n
    std = (m2 / n) ** 0.5 if n > 1 else 0.0
    return mean, std


def quantile_from_hist(counts: np.ndarray, q: float) -> float:
    """Delay-ratio quantile from a 16-bucket histogram.

    Linear interpolation within the containing bucket; the open-ended
    under/overflow buckets use virtual outer edges (UNDER_EDGE / OVER_EDGE).
    Returns NaN for an empty histogram.
    """
    counts = np.asarray(counts, dtype=float)
    total = counts.sum()
    if total == 0:
        return float("nan")
    target = q * total
    cum = 0.0
    lo_edges = np.concatenate([[UNDER_EDGE], HIST_EDGES])
    hi_edges = np.concatenate([HIST_EDGES, [OVER_EDGE]])
    for i in range(N_BUCKETS):
        c = counts[i]
        if c > 0 and cum + c >= target:
            frac = (target - cum) / c
            return float(lo_edges[i] + frac * (hi_edges[i] - lo_edges[i]))
        cum += c
    return float(hi_edges[-1])


# --------------------------------------------------------------------------
# Metric derivation from a combined accumulator (mirrored in JS)
# --------------------------------------------------------------------------

def derive_metrics(
    n: int, sum_delay: float, m2: float, hist: np.ndarray, t_ff_s: float
) -> dict:
    """All display metrics for one segment under one combined filter."""
    mean_d, std_d = mean_std(n, sum_delay, m2)
    r50 = quantile_from_hist(hist, 0.50)
    r90 = quantile_from_hist(hist, 0.90)
    return {
        "n": n,
        "mean_delay_s": mean_d,
        "std_delay_s": std_d,
        "median_delay_s": (r50 - 1.0) * t_ff_s if n else float("nan"),
        "p90_delay_s": (r90 - 1.0) * t_ff_s if n else float("nan"),
        "buffer_s": (r90 - r50) * t_ff_s if n else float("nan"),
    }


# --------------------------------------------------------------------------
# Golden vectors: JSON fixture the JS decoder tests against
# --------------------------------------------------------------------------

def emit_golden_vectors(out_path: Path, seed: int = 42) -> dict:
    rng = np.random.default_rng(seed)
    cases = []
    for i, (n, t_ff) in enumerate([(500, 60.0), (37, 25.0), (3, 120.0), (0, 40.0)]):
        ratios = rng.lognormal(mean=0.25, sigma=0.45, size=n)
        delays = (ratios - 1.0) * t_ff
        cn, cs, cm2 = welford_from_samples(delays)
        hist = hist_counts(ratios)
        # Split samples in two and merge, to pin down the merge path too.
        half = n // 2
        a = welford_from_samples(delays[:half])
        b = welford_from_samples(delays[half:])
        merged = welford_merge(*a, *b)
        cases.append(
            {
                "case": i,
                "t_ff_s": t_ff,
                "n": cn,
                "sum_delay": cs,
                "m2": cm2,
                "hist": hist.tolist(),
                "merged_equals_whole": [list(merged), [cn, cs, cm2]],
                "metrics": {
                    k: (None if isinstance(v, float) and np.isnan(v) else v)
                    for k, v in derive_metrics(cn, cs, cm2, hist, t_ff).items()
                },
            }
        )
    payload = {
        "hist_edges": HIST_EDGES.tolist(),
        "under_edge": UNDER_EDGE,
        "over_edge": OVER_EDGE,
        "cases": cases,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=1))
    return payload
