"""Shared trajectory representations + loaders for the clustering experiments.

Everything starts from the space-aligned profile matrix ``T`` (N trips x K
distance-grid points, seconds to reach each grid point from the 200 m mark)
built by ``build_dataset.py``. Because the route is fixed, aligning by
*distance* (not time) makes trips directly comparable pointwise; the dual,
time-aligned view d(t) is only needed by warping-based methods (DTW et al.).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

import config as C


def load_profiles() -> tuple[np.ndarray, np.ndarray, pd.DataFrame]:
    """(d_grid (K,), T (N,K), meta aligned to T's rows)."""
    z = np.load(C.PROFILE_NPZ, allow_pickle=True)
    d_grid, T, keys = z["d_grid"], z["T"], z["trip_keys"]
    meta = pd.read_csv(C.META_CSV).set_index("trip_key").loc[keys].reset_index()
    return d_grid, T, meta


def pace_profiles(T: np.ndarray, d_grid: np.ndarray) -> np.ndarray:
    """Per-bin pace (s/m): where each trip spends its time. Shape (N, K-1)."""
    return np.diff(T, axis=1) / np.diff(d_grid)[None, :]


def deviation_profiles(T: np.ndarray) -> np.ndarray:
    """Cumulative deviation from the corpus median curve (s). Shape (N, K).

    dev[i, k] = T[i, k] - median_j T[j, k]; captures both how slow a trip is
    and *where along the route* the time was gained/lost.
    """
    return T - np.median(T, axis=0, keepdims=True)


def time_view(T: np.ndarray, dt_s: float = 30.0) -> list[np.ndarray]:
    """Time-aligned dual d(t): distance sampled every ``dt_s`` seconds.

    Variable-length 1-D series (trips have different runtimes) for
    warping-based methods.
    """
    out = []
    for row in T:
        tt = np.arange(0.0, row[-1] + dt_s, dt_s)
        out.append(np.interp(tt, row, np.arange(len(row)) * C.GRID_STEP_M + C.GRID_D0_M))
    return out


def scalar_features(T: np.ndarray, d_grid: np.ndarray) -> pd.DataFrame:
    """Interpretable per-trip scalars (no time-of-day: that stays external
    so clusters can be *validated* against it)."""
    pace = pace_profiles(T, d_grid)  # s/m
    K = pace.shape[1]
    thirds = np.array_split(np.arange(K), 3)
    tot = T[:, -1]
    share = [pace[:, ix].sum(axis=1) * C.GRID_STEP_M / tot for ix in thirds]
    slow = pace > (1 / 2.0)  # slower than 2 m/s ~ crawling or stopped
    return pd.DataFrame({
        "runtime_s": tot,
        "share_first_third": share[0],
        "share_mid_third": share[1],
        "share_last_third": share[2],
        "slow_frac_dist": slow.mean(axis=1),
        "worst_bin_pace": pace.max(axis=1),
        "pace_var": pace.var(axis=1),
    })
