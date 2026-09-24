"""The five trajectory-reconstruction methods from Robbennolt et al. (2025).

All methods consume knots ``(t, x[, v])`` — time (s), monotone
distance-along-route (m), measured speed (m/s) — and return a
:class:`Hermite` spline with analytic position ``.pos(t)`` and velocity
``.vel(t)``.

Paper mapping (arXiv:2509.00119):

===============  =========================================================
PCHIP            Algorithm 1: tangents = secant means, Fritsch-Carlson
                 (FC) circle constraint. Position-only.
VCHIP-ME         section 2.2.3: tangents = measured velocities, then FC.
PCHIP-VCHIP      Algorithm 3: tangents = alpha*v + (1-alpha)*u where u are
                 the PCHIP tangents; then FC (== VCHIP-ME on blended v).
LOCREG-PCHIP     Algorithm 2: tricube-weighted local cubic regression of x
                 at the knots (k nearest neighbours), sequential monotone
                 clamp, then PCHIP on the smoothed knots.
LOCREG-PCHIP-V   Algorithm 4: LOCREG both x (k_x) and v (k_v), monotone
                 clamp with velocity consistency (eqs 32-33), then
                 VCHIP-ME on the smoothed knots.
===============  =========================================================

Measured speeds are clipped at 0 before use as tangents: FC monotonicity
requires tangent signs to match the (non-negative) secant signs.
"""

from __future__ import annotations

import numpy as np

EPS_FLAT = 1e-9  # "nearly flat interval" threshold from Algorithm 1


# ------------------------------------------------------------- FC core

def fritsch_carlson(t: np.ndarray, x: np.ndarray, m: np.ndarray) -> np.ndarray:
    """Apply Algorithm 1's monotonicity pass to tangents ``m`` (copy)."""
    m = np.asarray(m, dtype=float).copy()
    delta = np.diff(x) / np.diff(t)
    for k in range(len(delta)):
        if abs(delta[k]) < EPS_FLAT:
            m[k] = 0.0
            m[k + 1] = 0.0
        else:
            a = m[k] / delta[k]
            b = m[k + 1] / delta[k]
            if a * a + b * b > 9.0:
                tau = 3.0 / np.hypot(a, b)
                m[k] = tau * a * delta[k]
                m[k + 1] = tau * b * delta[k]
    return m


def _pchip_tangents(t: np.ndarray, x: np.ndarray) -> np.ndarray:
    """Paper init: endpoint secants, interior arithmetic mean of secants."""
    delta = np.diff(x) / np.diff(t)
    m = np.empty_like(x, dtype=float)
    m[0], m[-1] = delta[0], delta[-1]
    if len(x) > 2:
        m[1:-1] = (delta[:-1] + delta[1:]) / 2
    return fritsch_carlson(t, x, m)


class Hermite:
    """Piecewise cubic Hermite spline; clamps queries to the knot range."""

    def __init__(self, t: np.ndarray, x: np.ndarray, m: np.ndarray):
        self.t = np.asarray(t, dtype=float)
        self.x = np.asarray(x, dtype=float)
        self.m = np.asarray(m, dtype=float)

    def _locate(self, tq: np.ndarray):
        tq = np.clip(np.asarray(tq, dtype=float), self.t[0], self.t[-1])
        i = np.clip(np.searchsorted(self.t, tq, side="right") - 1, 0, len(self.t) - 2)
        h = self.t[i + 1] - self.t[i]
        s = (tq - self.t[i]) / h
        return i, h, s

    def pos(self, tq: np.ndarray) -> np.ndarray:
        i, h, s = self._locate(tq)
        s2, s3 = s * s, s * s * s
        return ((2 * s3 - 3 * s2 + 1) * self.x[i]
                + (s3 - 2 * s2 + s) * h * self.m[i]
                + (-2 * s3 + 3 * s2) * self.x[i + 1]
                + (s3 - s2) * h * self.m[i + 1])

    def vel(self, tq: np.ndarray) -> np.ndarray:
        i, h, s = self._locate(tq)
        s2 = s * s
        return ((6 * s2 - 6 * s) / h * self.x[i]
                + (3 * s2 - 4 * s + 1) * self.m[i]
                + (-6 * s2 + 6 * s) / h * self.x[i + 1]
                + (3 * s2 - 2 * s) * self.m[i + 1])


# ------------------------------------------------------------- LOCREG

def locreg_at_knots(t: np.ndarray, y: np.ndarray, k: int) -> np.ndarray:
    """Tricube-weighted local cubic regression evaluated at the knots.

    For each knot: the ``k`` nearest neighbours in time (a contiguous window
    since t is sorted), bandwidth = distance to the k-th neighbour, tricube
    weights, weighted cubic fit, prediction at the knot. Batched via normal
    equations; a tiny ridge keeps near-degenerate windows solvable.
    """
    t = np.asarray(t, dtype=float)
    y = np.asarray(y, dtype=float)
    n = len(t)
    k = int(min(max(k, 2), n))

    # contiguous k-NN window per knot
    starts = np.empty(n, dtype=int)
    for i in range(n):
        lo = max(0, i - k + 1)
        hi = min(n - k, i)      # window start in [lo, hi]
        best, bl = lo, np.inf
        for s0 in range(lo, hi + 1):
            span = max(t[i] - t[s0], t[s0 + k - 1] - t[i])
            if span < bl:
                bl, best = span, s0
        starts[i] = best

    idx = starts[:, None] + np.arange(k)[None, :]
    ti = t[idx] - t[:, None]                     # centred times (n, k)
    yi = y[idx]
    h = np.abs(ti).max(axis=1, keepdims=True)
    h = np.where(h <= 0, 1.0, h)
    u = np.abs(ti) / h
    w = np.clip(1 - u**3, 0, None) ** 3
    w = np.where(w <= 0, 1e-6, w)                # k-th neighbour sits at u=1

    deg = min(3, k - 1)
    A = ti[:, :, None] ** np.arange(deg + 1)[None, None, :]   # (n, k, d+1)
    Aw = A * w[:, :, None]
    G = np.einsum("nkd,nke->nde", Aw, A)
    G += 1e-9 * np.eye(deg + 1)[None]
    b = np.einsum("nkd,nk->nd", Aw, yi)
    theta = np.linalg.solve(G, b[..., None])[..., 0]
    return theta[:, 0]                            # prediction at centred t=0


def _monotone_clamp(y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Sequential y_i >= y_{i-1} clamp; returns (y, corrected mask)."""
    y = y.astype(float).copy()
    corrected = np.zeros(len(y), dtype=bool)
    for i in range(1, len(y)):
        if y[i] < y[i - 1]:
            y[i] = y[i - 1]
            corrected[i] = True
    return y, corrected


class Linear:
    """LSEG (paper 2.1.1): straight segments between pings; velocity is the
    piecewise-constant secant slope of the containing segment."""

    def __init__(self, t: np.ndarray, x: np.ndarray):
        self.t = np.asarray(t, dtype=float)
        self.x = np.asarray(x, dtype=float)
        self.delta = np.diff(self.x) / np.diff(self.t)

    def pos(self, tq: np.ndarray) -> np.ndarray:
        return np.interp(np.asarray(tq, dtype=float), self.t, self.x)

    def vel(self, tq: np.ndarray) -> np.ndarray:
        tq = np.clip(np.asarray(tq, dtype=float), self.t[0], self.t[-1])
        i = np.clip(np.searchsorted(self.t, tq, side="right") - 1,
                    0, len(self.t) - 2)
        return self.delta[i]


# ------------------------------------------------------------- methods

def lseg(t, x, v=None, **_) -> Linear:
    return Linear(t, x)


def pchip(t, x, v=None, **_) -> Hermite:
    return Hermite(t, x, _pchip_tangents(np.asarray(t, float), np.asarray(x, float)))


def vchip_me(t, x, v, **_) -> Hermite:
    t = np.asarray(t, float)
    x = np.asarray(x, float)
    m = np.clip(np.asarray(v, float), 0.0, None)
    return Hermite(t, x, fritsch_carlson(t, x, m))


def pchip_vchip(t, x, v, alpha: float = 0.5, **_) -> Hermite:
    t = np.asarray(t, float)
    x = np.asarray(x, float)
    u = _pchip_tangents(t, x)
    blended = alpha * np.clip(np.asarray(v, float), 0.0, None) + (1 - alpha) * u
    return Hermite(t, x, fritsch_carlson(t, x, blended))


def locreg_pchip(t, x, v=None, k: int = 10, **_) -> Hermite:
    t = np.asarray(t, float)
    y = locreg_at_knots(t, np.asarray(x, float), k)
    y, _corr = _monotone_clamp(y)
    return Hermite(t, y, _pchip_tangents(t, y))


def locreg_pchip_v(t, x, v, kx: int = 10, kv: int = 10, **_) -> Hermite:
    t = np.asarray(t, float)
    xs = locreg_at_knots(t, np.asarray(x, float), kx)
    vs = locreg_at_knots(t, np.asarray(v, float), kv)
    y, corrected = _monotone_clamp(xs)
    u = np.clip(vs, 0.0, None)
    n = len(t)
    for i in np.where(corrected)[0]:              # eq (33)
        if 1 <= i <= n - 2:
            u[i] = max((y[i + 1] - y[i - 1]) / (t[i + 1] - t[i - 1]), 0.0)
    return Hermite(t, y, fritsch_carlson(t, y, u))


def _vspline_theta(t, x, v, gamma: float, eta: float):
    """Paper eqs (34)-(42): joint smoothing of knot positions AND velocities.

    theta = [x1, v1, ..., xn, vn] minimizing
        ||pos residuals||^2 + gamma*||vel residuals||^2 + n * theta' Omega theta
    with the cubic-Hermite curvature Gram matrix Omega (banded, bandwidth 3)
    and adaptive weights lambda_i = eta * h_i / v_avg_i^2 (eq 41; v_avg
    floored at 0.3 m/s so stopped intervals stay finite). Solved with a
    banded Cholesky -> O(n), not the paper's dense O(n^3).
    """
    from scipy.linalg import solveh_banded
    n = len(t)
    N = 2 * n
    ab = np.zeros((4, N))          # upper banded form, bandwidth 3
    rhs = np.zeros(N)
    ab[3, 0::2] += 1.0             # position observations
    ab[3, 1::2] += gamma           # velocity observations
    rhs[0::2] = x
    rhs[1::2] = gamma * v
    h = np.diff(t)
    v_avg = np.abs(np.diff(x)) / h
    lam = n * eta * h / np.maximum(v_avg, 0.3) ** 2
    for i in range(n - 1):
        hi, li = h[i], lam[i]
        K = li * np.array([
            [12 / hi**3,  6 / hi**2, -12 / hi**3,  6 / hi**2],
            [6 / hi**2,   4 / hi,    -6 / hi**2,   2 / hi],
            [-12 / hi**3, -6 / hi**2, 12 / hi**3, -6 / hi**2],
            [6 / hi**2,   2 / hi,    -6 / hi**2,   4 / hi]])
        base = 2 * i
        for a in range(4):
            for b in range(a, 4):
                ab[3 - (b - a), base + b] += K[a, b]
    theta = solveh_banded(ab, rhs)
    return theta[0::2], theta[1::2]


def vspline_me(t, x, v, gamma: float = 10.0, eta: float = 0.1, **_) -> Hermite:
    """V-SPLINE-ME (paper 2.2.9): V-SPLINE smoothing, then the same
    monotonicity + consistency pass as LOCREG-PCHIP-V (eqs 32-33 + FC)."""
    t = np.asarray(t, float)
    xs, vs = _vspline_theta(t, np.asarray(x, float),
                            np.clip(np.asarray(v, float), 0.0, None),
                            gamma, eta)
    y, corrected = _monotone_clamp(xs)
    u = np.clip(vs, 0.0, None)
    n = len(t)
    for i in np.where(corrected)[0]:
        if 1 <= i <= n - 2:
            u[i] = max((y[i + 1] - y[i - 1]) / (t[i + 1] - t[i - 1]), 0.0)
    return Hermite(t, y, fritsch_carlson(t, y, u))


BUILDERS = {
    "LSEG": lseg,
    "PCHIP": pchip,
    "VCHIP-ME": vchip_me,
    "PCHIP-VCHIP": pchip_vchip,
    "LOCREG-PCHIP": locreg_pchip,
    "LOCREG-PCHIP-V": locreg_pchip_v,
    "V-SPLINE-ME": vspline_me,
}

# Which methods take which tunable parameters (tuning grids in config.py).
TUNABLE = {
    "LSEG": (),
    "PCHIP": (),
    "VCHIP-ME": (),
    "PCHIP-VCHIP": ("alpha",),
    "LOCREG-PCHIP": ("k",),
    "LOCREG-PCHIP-V": ("kx", "kv"),
    "V-SPLINE-ME": ("gamma", "eta"),
}
