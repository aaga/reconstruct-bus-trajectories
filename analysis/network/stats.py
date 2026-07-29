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

# Histograms: bucket 0 = underflow (< EDGES[0]), buckets 1..14 between the
# 15 edges, bucket 15 = overflow (>= EDGES[-1]). Under/over "virtual" edges
# bound in-bucket interpolation in the open-ended buckets.
N_BUCKETS = 16

# Overall + non-dwell delay: ratio to free-flow (t/t_ff), log-spaced.
RATIO_LO = 0.75
RATIO_HI = 6.0
HIST_EDGES = np.geomspace(RATIO_LO, RATIO_HI, N_BUCKETS - 1)  # 15 edges
UNDER_EDGE = 0.5
OVER_EDGE = 8.0

# Dwell: ratio dwell/t_ff in [0, ~3): first edge 0.02 (bucket 0 ≈ "no dwell").
DWELL_EDGES = np.geomspace(0.02, 3.0, N_BUCKETS - 1)
DWELL_UNDER = 0.0
DWELL_OVER = 5.0

# Passenger-weighted non-dwell delay, ABSOLUTE pax-seconds (no t_ff scaling):
# bucket 0 = negative (running ahead of free-flow), log buckets 5 .. 20000.
PAX_EDGES = np.concatenate([[0.0], np.geomspace(5.0, 20000.0, N_BUCKETS - 2)])
PAX_UNDER = -400.0
PAX_OVER = 60000.0

HIST_FAMILIES = {
    # name -> (edges, under, over)
    "ratio": (HIST_EDGES, UNDER_EDGE, OVER_EDGE),
    "nd": (HIST_EDGES, UNDER_EDGE, OVER_EDGE),
    "dw": (DWELL_EDGES, DWELL_UNDER, DWELL_OVER),
    "pax": (PAX_EDGES, PAX_UNDER, PAX_OVER),
}


def hist_index(values: np.ndarray, edges: np.ndarray = HIST_EDGES) -> np.ndarray:
    """Bucket index (0..15) for each value against a family's edges."""
    return np.searchsorted(edges, np.asarray(values, dtype=float), side="right")


def hist_counts(values: np.ndarray, edges: np.ndarray = HIST_EDGES) -> np.ndarray:
    """16-bucket histogram of values against a family's edges."""
    return np.bincount(hist_index(values, edges), minlength=N_BUCKETS).astype(np.int64)


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


def quantile_from_hist(
    counts: np.ndarray,
    q: float,
    edges: np.ndarray = HIST_EDGES,
    under: float = UNDER_EDGE,
    over: float = OVER_EDGE,
) -> float:
    """Quantile from a 16-bucket histogram (any family's edges).

    Linear interpolation within the containing bucket; the open-ended
    under/overflow buckets use the family's virtual outer edges.
    Returns NaN for an empty histogram.
    """
    counts = np.asarray(counts, dtype=float)
    total = counts.sum()
    if total == 0:
        return float("nan")
    target = q * total
    cum = 0.0
    lo_edges = np.concatenate([[under], edges])
    hi_edges = np.concatenate([edges, [over]])
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
    n: int,
    sum_delay: float,
    m2: float,
    hist: np.ndarray,
    t_ff_s: float,
    *,
    n_door: int = 0,
    sum_dwell: float = 0.0,
    sum_delay_door: float = 0.0,
) -> dict:
    """All display metrics for one segment under one combined filter.

    Door metrics use ONLY the door-covered subset (n_door traversals): the
    at-stop/in-motion split must come from the same trips or it doesn't sum.
    """
    mean_d, std_d = mean_std(n, sum_delay, m2)
    r50 = quantile_from_hist(hist, 0.50)
    r90 = quantile_from_hist(hist, 0.90)
    out = {
        "n": n,
        "mean_delay_s": mean_d,
        "std_delay_s": std_d,
        "median_delay_s": (r50 - 1.0) * t_ff_s if n else float("nan"),
        "p90_delay_s": (r90 - 1.0) * t_ff_s if n else float("nan"),
        "buffer_s": (r90 - r50) * t_ff_s if n else float("nan"),
        "n_door": n_door,
        "mean_dwell_s": float("nan"),
        "moving_delay_s": float("nan"),
        "dwell_share": float("nan"),
    }
    if n_door > 0:
        mean_dwell = sum_dwell / n_door
        mean_delay_door = sum_delay_door / n_door
        out["mean_dwell_s"] = mean_dwell
        out["moving_delay_s"] = mean_delay_door - mean_dwell
        out["dwell_share"] = (
            sum_dwell / sum_delay_door if sum_delay_door > 1e-9 else float("nan")
        )
    return out


# --------------------------------------------------------------------------
# Per-(family, stat) derivation — the UI contract (mirrored in JS)
# --------------------------------------------------------------------------
#
# Families ("delay" selectors in the metric dropdown):
#   overall  — delay = t_obs − t_ff, all traversals (n, sum_delay, m2, hist ratio)
#   pax      — passenger-weighted non-dwell delay in pax-seconds (door subset)
#   nondwell — delay − dwell, seconds (door subset)
#   dwell    — door-open seconds (door subset)
# Stats: mean | median | std | p95 | buffer (p95 − mean)

def derive_stat(family: str, stat: str, acc: dict, t_ff_s: float) -> float:
    """acc keys: n, sum, m2, hist (ratio) | n_door, sum_dwell, sum_delay_door,
    m2_dw, m2_nd, hist_dw, hist_nd | sum_pax, m2_pax, hist_pax."""

    def block(n, total, m2, hist, edges, under, over, to_seconds):
        if not n:
            return float("nan")
        mean = total / n
        if stat == "mean":
            return mean
        if stat == "std":
            return (m2 / n) ** 0.5 if n > 1 else 0.0
        if stat in ("median", "p95", "buffer"):
            q = 0.5 if stat == "median" else 0.95
            v = quantile_from_hist(np.asarray(hist), q, edges, under, over)
            v = to_seconds(v)
            return v - mean if stat == "buffer" else v
        raise ValueError(stat)

    if family == "overall":
        return block(acc["n"], acc["sum"], acc["m2"], acc["hist"],
                     HIST_EDGES, UNDER_EDGE, OVER_EDGE,
                     lambda r: (r - 1.0) * t_ff_s)
    nd = acc.get("n_door", 0)
    if family == "dwell":
        return block(nd, acc.get("sum_dwell", 0.0), acc.get("m2_dw", 0.0),
                     acc.get("hist_dw"), DWELL_EDGES, DWELL_UNDER, DWELL_OVER,
                     lambda r: r * t_ff_s)
    if family == "nondwell":
        return block(nd, acc.get("sum_delay_door", 0.0) - acc.get("sum_dwell", 0.0),
                     acc.get("m2_nd", 0.0), acc.get("hist_nd"),
                     HIST_EDGES, UNDER_EDGE, OVER_EDGE,
                     lambda r: (r - 1.0) * t_ff_s)
    if family == "pax":
        return block(nd, acc.get("sum_pax", 0.0), acc.get("m2_pax", 0.0),
                     acc.get("hist_pax"), PAX_EDGES, PAX_UNDER, PAX_OVER,
                     lambda v: v)
    raise ValueError(family)


# --------------------------------------------------------------------------
# Golden vectors: JSON fixture the JS decoder tests against
# --------------------------------------------------------------------------

def emit_golden_vectors(out_path: Path, seed: int = 42) -> dict:
    rng = np.random.default_rng(seed)
    families = ["overall", "pax", "nondwell", "dwell"]
    stats = ["mean", "median", "std", "p95", "buffer"]
    cases = []
    for i, (n, t_ff) in enumerate([(500, 60.0), (37, 25.0), (3, 120.0), (0, 40.0)]):
        ratios = rng.lognormal(mean=0.25, sigma=0.45, size=n)
        delays = (ratios - 1.0) * t_ff
        cn, cs, cm2 = welford_from_samples(delays)

        n_door = int(n * 0.6)
        dwell = np.abs(rng.normal(8.0, 6.0, n_door))
        nd_delay = delays[:n_door] - dwell
        loads = rng.integers(0, 40, n_door).astype(float)
        pax = nd_delay * loads

        acc = {
            "n": cn, "sum": cs, "m2": cm2,
            "hist": hist_counts(ratios).tolist(),
            "n_door": n_door,
            "sum_dwell": float(dwell.sum()),
            "sum_delay_door": float(delays[:n_door].sum()),
            "m2_dw": welford_from_samples(dwell)[2],
            "m2_nd": welford_from_samples(nd_delay)[2],
            "hist_dw": hist_counts(dwell / t_ff, DWELL_EDGES).tolist(),
            "hist_nd": hist_counts((nd_delay + t_ff) / t_ff, HIST_EDGES).tolist(),
            "sum_pax": float(pax.sum()),
            "m2_pax": welford_from_samples(pax)[2],
            "hist_pax": hist_counts(pax, PAX_EDGES).tolist(),
        }
        expected = {}
        for fam in families:
            for st in stats:
                v = derive_stat(fam, st, acc, t_ff)
                expected[f"{fam}.{st}"] = None if np.isnan(v) else round(float(v), 6)
        cases.append({"case": i, "t_ff_s": t_ff, "acc": acc, "expected": expected})

    payload = {
        "families": {
            k: {"edges": list(map(float, e)), "under": u, "over": o}
            for k, (e, u, o) in HIST_FAMILIES.items()
        },
        "cases": cases,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=1))
    return payload
