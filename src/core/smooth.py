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
from scipy.interpolate import BPoly, CubicSpline, PchipInterpolator

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


def _prepare_knots(
    t: np.ndarray,
    d: np.ndarray,
    bandwidth: int,
    degree: int,
):
    """Shared LOCREG → monotonicity → unique-knot prep for the spline fitters.

    Returns ``(t_input, d_input, x_in_input_order, unique_t, x_for_spline)``:
    the LOCREG-smoothed, monotonized values in the caller's input order plus the
    strictly-increasing ``(unique_t, x_for_spline)`` knots an interpolator needs.
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

    # Interpolators require strictly increasing t; collapse exact-duplicate
    # timestamps by averaging their x values (rare, but possible if AVL emits
    # two pings at the same millisecond).
    unique_t, inv = np.unique(ts, return_inverse=True)
    if unique_t.size != ts.size:
        x_for_spline = np.zeros_like(unique_t)
        counts = np.zeros_like(unique_t)
        np.add.at(x_for_spline, inv, x_mono)
        np.add.at(counts, inv, 1.0)
        x_for_spline /= counts
        # Re-enforce monotonicity post-averaging (cheap).
        x_for_spline = enforce_monotonic(x_for_spline)
    else:
        unique_t = ts
        x_for_spline = x_mono

    x_in_input_order = np.empty_like(x_mono)
    x_in_input_order[order] = x_mono
    return t, d, x_in_input_order, unique_t, x_for_spline


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
    t, d, x_in_input_order, unique_t, x_for_spline = _prepare_knots(
        t, d, bandwidth, degree
    )
    f = PchipInterpolator(unique_t, x_for_spline, extrapolate=False)
    return LocregPchipResult(t=t, d_raw=d, x=x_in_input_order, f=f)


def locreg_mqsi(
    t: np.ndarray,
    d: np.ndarray,
    bandwidth: int = DEFAULT_BANDWIDTH,
    degree: int = DEFAULT_DEGREE,
) -> LocregPchipResult:
    """End-to-end: LOCREG → monotonicity forward-fill → C² monotone quintic.

    The PCHIP variant (:func:`locreg_pchip`) is monotone and ``C^1`` but its
    acceleration ``f''`` jumps at every knot. This variant instead fits a
    ``C^2`` quintic Hermite spline so acceleration is continuous: at each knot it
    matches the value, the PCHIP (Fritsch–Carlson) monotone first derivative,
    and a cubic-spline second derivative. Because consecutive intervals share
    value/1st/2nd derivatives at each knot, the result is ``C^2``; the monotone
    first derivatives keep it visually monotone for trajectory data.

    This is a practical monotonicity-aware quintic, not the full TOMS-1031 MQSI
    algorithm; it exists to render the PCHIP-vs-MQSI smoothness comparison.

    Returns a :class:`LocregPchipResult` whose ``f`` is a quintic
    :class:`scipy.interpolate.BPoly` (supports ``.derivative(1)``/``(2)``).
    """
    t, d, x_in_input_order, unique_t, x_for_spline = _prepare_knots(
        t, d, bandwidth, degree
    )
    # First derivatives: PCHIP's monotone Fritsch–Carlson slopes.
    m = PchipInterpolator(unique_t, x_for_spline).derivative(1)(unique_t)
    # Second derivatives: from a natural cubic spline (gives C² acceleration).
    s = CubicSpline(unique_t, x_for_spline, bc_type="natural").derivative(2)(unique_t)
    # Quintic Hermite: match (value, 1st, 2nd) at every knot → C² by construction.
    knot_derivs = [[x_for_spline[i], m[i], s[i]] for i in range(unique_t.size)]
    f = BPoly.from_derivatives(unique_t, knot_derivs, extrapolate=False)
    return LocregPchipResult(t=t, d_raw=d, x=x_in_input_order, f=f)


# --------------------------------------------------------------------------
# Slowscope reconstruction methods (2026-08, UT-Austin comparative study):
# VCHIP-ME where the feed reports speeds, plain PCHIP (no LOCREG) elsewhere.
# --------------------------------------------------------------------------

def _dedupe_mono(t: np.ndarray, d: np.ndarray, v: np.ndarray | None):
    """Sort by t, collapse duplicate timestamps (keep first), forward-fill
    position monotonicity. Returns strictly-increasing knots."""
    t = np.asarray(t, dtype=float)
    d = np.asarray(d, dtype=float)
    if t.shape != d.shape:
        raise ValueError("t and d must have the same shape")
    if t.size < 2:
        raise ValueError("need at least 2 points to interpolate")
    order = np.argsort(t, kind="stable")
    t, d = t[order], d[order]
    if v is not None:
        v = np.asarray(v, dtype=float)[order]
    keep = np.concatenate([[True], np.diff(t) > 0])
    t, d = t[keep], d[keep]
    if v is not None:
        v = v[keep]
    if t.size < 2:
        raise ValueError("fewer than 2 distinct timestamps")
    return t, enforce_monotonic(d), v


def plain_pchip(t: np.ndarray, d: np.ndarray) -> LocregPchipResult:
    """Monotone PCHIP straight through the (monotonized) observations —
    the no-speed fallback. No LOCREG smoothing stage."""
    tu, xu, _ = _dedupe_mono(t, d, None)
    f = PchipInterpolator(tu, xu, extrapolate=False)
    x_in = np.interp(np.asarray(t, dtype=float), tu, xu)
    return LocregPchipResult(t=np.asarray(t, float), d_raw=np.asarray(d, float),
                             x=x_in, f=f)


def vchip_me(t: np.ndarray, d: np.ndarray, v: np.ndarray,
             flat_eps: float = 1e-9) -> LocregPchipResult:
    """Velocity-Constrained Hermite Interpolation with Monotonicity
    Enforcement (Robbennolt, Munira & Boyles 2025, §2.2.3).

    Tangents are initialized to the OBSERVED velocities (m/s) instead of
    secant averages, then constrained exactly as Fritsch–Carlson:

      * nearly-flat interval (|δ_k| < ε) → both endpoint tangents zeroed
        (a dwell holds still and leaves it at rest);
      * else α = m_k/δ_k, β = m_{k+1}/δ_k must satisfy α²+β² ≤ 9, enforced
        by scaling both with τ = 3/√(α²+β²).

    Non-finite velocities (feed sentinel / missing) fall back to the PCHIP
    secant-average initialization for those knots. Result is C¹, matches
    positions everywhere and observed speeds wherever the constraint is
    inactive, and is monotone non-decreasing by the F–C guarantee.
    """
    from scipy.interpolate import CubicHermiteSpline

    tu, xu, vu = _dedupe_mono(t, d, v)
    n = tu.size
    dt = np.diff(tu)
    delta = np.diff(xu) / dt

    m = np.asarray(vu, dtype=float).copy()
    # sentinel/missing speeds -> PCHIP-style secant initialization
    bad = ~np.isfinite(m)
    if bad.any():
        init = np.empty(n)
        init[0] = delta[0]
        init[-1] = delta[-1]
        if n > 2:
            init[1:-1] = (delta[:-1] + delta[1:]) / 2
        m[bad] = init[bad]
    np.clip(m, 0.0, None, out=m)  # forward travel only

    for k in range(n - 1):
        if abs(delta[k]) < flat_eps:
            m[k] = 0.0
            m[k + 1] = 0.0
        else:
            a = m[k] / delta[k]
            b = m[k + 1] / delta[k]
            r2 = a * a + b * b
            if r2 > 9.0:
                tau = 3.0 / np.sqrt(r2)
                m[k] = tau * a * delta[k]
                m[k + 1] = tau * b * delta[k]

    f = CubicHermiteSpline(tu, xu, m, extrapolate=False)
    x_in = np.interp(np.asarray(t, dtype=float), tu, xu)
    return LocregPchipResult(t=np.asarray(t, float), d_raw=np.asarray(d, float),
                             x=x_in, f=f)


def fit_trajectory(t: np.ndarray, d: np.ndarray,
                   v: np.ndarray | None = None) -> LocregPchipResult:
    """Route to the reconstruction method for this feed (2026-08 decision):
    VCHIP-ME when usable speeds are present (>= half the pings finite),
    plain PCHIP otherwise. LOCREG is retired from the network pipeline."""
    if v is not None:
        v = np.asarray(v, dtype=float)
        if np.isfinite(v).mean() >= 0.5:
            return vchip_me(t, d, v)
    return plain_pchip(t, d)
