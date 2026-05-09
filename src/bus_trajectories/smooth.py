"""LOCREG-PCHIP trajectory smoother.

Implements Algorithm 1 from Huang et al. (ITSC 2023, §III-C):

  1. LOCREG: at each timestamp ``t_i`` fit a degree-``p`` polynomial in time to
     the ``bandwidth`` nearest neighbours weighted by the tricube kernel; take
     the fitted value at ``t_i`` as ``x_i``.
  2. Forward-fill any monotonicity violation: if ``x_i < x_{i-1}`` set
     ``x_i = x_{i-1}``.
  3. Fit a PCHIP (monotone cubic Hermite) interpolant through ``(t_i, x_i)``.

The returned :class:`scipy.interpolate.PchipInterpolator` is monotone
non-decreasing and ``C^1`` continuous; its first derivative is the speed
profile, second derivative is acceleration (not guaranteed smooth).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.interpolate import PchipInterpolator

DEFAULT_BANDWIDTH = 20  # paper's empirical choice
DEFAULT_DEGREE = 3  # cubic local polynomial


def tricube(u: np.ndarray) -> np.ndarray:
    """Tricube kernel ``(1 - |u|^3)^3`` for ``|u| < 1``, else 0."""
    u = np.abs(u)
    w = np.zeros_like(u)
    mask = u < 1.0
    w[mask] = (1.0 - u[mask] ** 3) ** 3
    return w


@dataclass
class LocregPchipResult:
    t: np.ndarray  # input timestamps (seconds since trip start)
    d_raw: np.ndarray  # raw distance-along-route (meters)
    x: np.ndarray  # post-LOCREG, post-monotonicity forward-fill (meters)
    f: PchipInterpolator  # the smoothed trajectory


def locreg(
    t: np.ndarray,
    d: np.ndarray,
    bandwidth: int = DEFAULT_BANDWIDTH,
    degree: int = DEFAULT_DEGREE,
) -> np.ndarray:
    """Local polynomial regression evaluated at every input timestamp.

    Returns ``x`` of shape ``(n,)``: the LOCREG fit at each ``t_i``. Uses the
    ``bandwidth`` nearest neighbours in ``t`` (paper's choice = 20) and a
    tricube kernel scaled to the largest in-window distance.
    """
    t = np.asarray(t, dtype=float)
    d = np.asarray(d, dtype=float)
    n = t.shape[0]
    if n != d.shape[0]:
        raise ValueError("t and d must have the same length")
    if n == 0:
        return np.array([])

    bw = min(bandwidth, n)
    out = np.empty(n, dtype=float)

    # ``t`` should be sorted for trip pings, but we don't rely on it.
    order = np.argsort(t)
    ts = t[order]
    ds = d[order]
    out_sorted = np.empty(n, dtype=float)

    for i in range(n):
        # bw nearest indices in sorted-time array (window slides with i).
        # Find the contiguous bw-window of indices in [0, n) closest to i.
        lo = max(0, i - bw // 2)
        hi = lo + bw
        if hi > n:
            hi = n
            lo = max(0, hi - bw)
        idx = np.arange(lo, hi)
        tw = ts[idx]
        dw = ds[idx]
        h = max(np.abs(tw - ts[i]).max(), 1e-9)
        w = tricube((tw - ts[i]) / h)
        # Centered design matrix improves numerical stability.
        deg = min(degree, len(idx) - 1)
        if deg < 1:
            out_sorted[i] = dw[0]
            continue
        x = tw - ts[i]
        # weighted polynomial fit
        # np.polyfit accepts weights as 1/sigma, but it minimizes
        # sum(w_k * (y - p(x))^2) when you pass w_k via the weights argument
        # since v >= 1.7. Use sqrt(w) explicitly via lstsq for clarity.
        sw = np.sqrt(w)
        V = np.vander(x, N=deg + 1)  # columns: x^deg, ..., x^1, 1
        Vw = V * sw[:, None]
        yw = dw * sw
        coeffs, *_ = np.linalg.lstsq(Vw, yw, rcond=None)
        # Evaluate at ts[i] which is x = 0 → constant term.
        out_sorted[i] = coeffs[-1]

    # un-sort
    out[order] = out_sorted
    return out


def enforce_monotonic(x: np.ndarray) -> np.ndarray:
    """Forward-fill any decrease: ``x_i := max(x_i, x_{i-1})``."""
    return np.maximum.accumulate(np.asarray(x, dtype=float))


def locreg_pchip(
    t: np.ndarray,
    d: np.ndarray,
    bandwidth: int = DEFAULT_BANDWIDTH,
    degree: int = DEFAULT_DEGREE,
) -> LocregPchipResult:
    """End-to-end: LOCREG → monotonicity forward-fill → PCHIP.

    Inputs are assumed to be sorted by ``t``; if not, they are sorted internally
    and the returned arrays are reordered to match the input order.
    """
    t = np.asarray(t, dtype=float)
    d = np.asarray(d, dtype=float)
    if t.shape != d.shape:
        raise ValueError("t and d must have the same shape")
    if t.size < 2:
        raise ValueError("need at least 2 points to interpolate")

    order = np.argsort(t)
    ts = t[order]
    ds = d[order]

    # Step 1: LOCREG fit.
    x_lr = locreg(ts, ds, bandwidth=bandwidth, degree=degree)
    # Step 2: forward-fill monotonicity violations.
    x_mono = enforce_monotonic(x_lr)

    # PCHIP requires strictly increasing t; collapse exact-duplicate timestamps
    # by averaging their x values (rare, but possible if AVL emits two pings at
    # the same millisecond).
    unique_t, inv = np.unique(ts, return_inverse=True)
    if unique_t.size != ts.size:
        x_for_pchip = np.zeros_like(unique_t)
        counts = np.zeros_like(unique_t)
        np.add.at(x_for_pchip, inv, x_mono)
        np.add.at(counts, inv, 1.0)
        x_for_pchip /= counts
        # Re-enforce monotonicity post-averaging (cheap).
        x_for_pchip = enforce_monotonic(x_for_pchip)
    else:
        unique_t = ts
        x_for_pchip = x_mono

    f = PchipInterpolator(unique_t, x_for_pchip, extrapolate=False)

    # Re-order to input index space.
    x_in_input_order = np.empty_like(x_mono)
    x_in_input_order[order] = x_mono
    return LocregPchipResult(t=t, d_raw=d, x=x_in_input_order, f=f)
