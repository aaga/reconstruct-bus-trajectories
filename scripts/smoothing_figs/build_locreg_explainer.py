"""LOCREG explainer slide: the algorithm + a worked example with the cubic
that comes out of weighted least-squares.

Output: slides/L_locreg_explainer.png
"""

from __future__ import annotations

import sys
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

PYTHONPATH_SRC = str((Path(__file__).resolve().parents[2] / "src"))
if PYTHONPATH_SRC not in sys.path:
    sys.path.insert(0, PYTHONPATH_SRC)

from bus_trajectories.smooth import tricube  # noqa: E402

SLIDES = Path("figures")
M_PER_MI = 1609.344


def main() -> None:
    SLIDES.mkdir(exist_ok=True)

    # Five sample pings — bandwidth = 5 (matches our presentation pipeline).
    t = np.array([0.0, 28.0, 55.0, 82.0, 110.0])
    d = np.array([0.0, 138.0, 290.0, 425.0, 562.0])
    i = 1
    bw = 5
    deg = 3

    x = t - t[i]
    h = max(np.abs(x).max(), 1e-9)
    u = x / h
    w = tricube(u)
    sw = np.sqrt(w)
    V = np.vander(x, N=deg + 1)
    Vw = V * sw[:, None]
    yw = d * sw
    coeffs, *_ = np.linalg.lstsq(Vw, yw, rcond=None)
    a3, a2, a1, a0 = coeffs

    # Plot
    fig = plt.figure(figsize=(15, 9.2), dpi=160)
    fig.patch.set_facecolor("white")
    gs = fig.add_gridspec(1, 2, width_ratios=[1.4, 1.0], wspace=0.18,
                           left=0.04, right=0.97, top=0.93, bottom=0.06)
    ax_text = fig.add_subplot(gs[0, 0]); ax_text.axis("off")
    ax_plot = fig.add_subplot(gs[0, 1])

    fig.suptitle("LOCREG step — local weighted cubic regression",
                 fontsize=15, y=0.98)

    eq_lines = [
        (r"$\mathbf{Algorithm\ (per\ ping\ } i \mathbf{)}$", 14, "#111"),
        ("", 6, "#111"),
        (r"1.  Pick the $k=5$ nearest pings in time around $i$.", 12, "#222"),
        (r"2.  Center time:  $x_k = t_k - t_i$.", 12, "#222"),
        (r"3.  Bandwidth scale:  $h = \max_k |x_k|$.", 12, "#222"),
        (r"4.  Tricube weights:  $w_k = (1 - |x_k/h|^3)^3$.",
         12, "#222"),
        (r"5.  Weighted LS fit of degree $p=3$:", 12, "#222"),
        (r"        $\hat a = \mathrm{argmin}_a \sum_k w_k\,"
         r"(d_k - p(x_k;\,a))^2$", 12, "#222"),
        (r"        $p(x) = a_3 x^3 + a_2 x^2 + a_1 x + a_0$", 12, "#222"),
        (r"6.  Smoothed value:  $\tilde d_i := p(0) = a_0$.", 12, "#222"),
        ("", 10, "#111"),
        (r"$\mathbf{Why\ a\ cubic?}$  A cubic captures local position +"
         r" velocity + curvature", 11, "#444"),
        (r"+ jerk with a 4-parameter fit. The kernel down-weights distant"
         r" pings;", 11, "#444"),
        (r"the boundary points get weight 0.", 11, "#444"),
        ("", 14, "#111"),
        (r"$\mathbf{Worked\ example\ (}i = 1\mathbf{)}$", 14, "#111"),
        ("", 4, "#111"),
        (r"   $t$ (s):  0,  28,  55,  82,  110", 12, "#222"),
        (r"   $d$ (m):  0, 138, 290, 425, 562", 12, "#222"),
        ("", 4, "#111"),
        (r"   $x_k = t_k - t_1$:  $-28,\ 0,\ 27,\ 54,\ 82$", 12, "#222"),
        (r"   $h = \max|x_k| = 82$", 12, "#222"),
        (r"   $|x_k/h|$:  $0.341,\ 0,\ 0.329,\ 0.659,\ 1.000$", 12, "#222"),
        (r"   $w_k$:  $0.886,\ 1.000,\ 0.897,\ 0.365,\ 0.000$", 12, "#222"),
        ("", 6, "#111"),
        (r"   Solve $V_w\,a = y_w$ where $V_{w,kj} = \sqrt{w_k}\,x_k^{\,p-j}$"
         r" and  $y_{w,k} = \sqrt{w_k}\,d_k$:", 12, "#222"),
        ("", 4, "#111"),
        (rf"   $\hat a_3 = {a3:+.4e}$", 12, "#222"),
        (rf"   $\hat a_2 = {a2:+.4e}$", 12, "#222"),
        (rf"   $\hat a_1 = {a1:+.4e}$", 12, "#222"),
        (rf"   $\hat a_0 = {a0:+.4f}$  $\leftarrow$ "
         rf"$\mathbf{{LOCREG\ output\ \tilde d_1}}$", 12, "#cc0000"),
        ("", 8, "#111"),
        (rf"   $p(x) = {a3:+.3e}\,x^3 "
         rf"{a2:+.3e}\,x^2 "
         rf"{a1:+.3f}\,x "
         rf"{a0:+.2f}$", 11.5, "#222"),
        ("", 4, "#111"),
        (rf"   Local velocity at $t_1$:   $p'(0) = a_1 = "
         rf"{a1:.2f}$ m/s  ($\approx {a1*2.23694:.1f}$ mph)", 12, "#222"),
    ]

    y_cursor = 0.97
    for txt, sz, col in eq_lines:
        if txt == "":
            y_cursor -= sz / 250
            continue
        ax_text.text(0.0, y_cursor, txt, fontsize=sz, color=col,
                     transform=ax_text.transAxes, va="top", ha="left")
        y_cursor -= (sz + 5) / 250

    # Right panel: data + fitted cubic + tricube kernel.
    ax_plot.set_facecolor("#fafbfc")
    for s in ("top", "right"):
        ax_plot.spines[s].set_visible(False)
    ax_plot.set_title(rf"Local cubic fit at $t_{{i}} = t_1 = 28$ s",
                      fontsize=12, pad=8)
    ax_plot.set_xlabel(r"time $t$ (s)", fontsize=11)
    ax_plot.set_ylabel(r"distance along route $d$ (m)", fontsize=11)

    # Fitted polynomial as a curve
    t_fine = np.linspace(t.min() - 5, t.max() + 5, 400)
    x_fine = t_fine - t[i]
    p_fine = a3 * x_fine**3 + a2 * x_fine**2 + a1 * x_fine + a0
    ax_plot.plot(t_fine, p_fine, color="#1f77b4", linewidth=2.0,
                  zorder=4, label=r"fitted $p(x)$")

    # Pings, sized by tricube weight
    sizes = 80 + 320 * w
    sc = ax_plot.scatter(t, d, s=sizes, color="#666", edgecolor="black",
                         linewidth=0.8, zorder=5, alpha=0.9,
                         label="pings (size = $w_k$)")

    # Highlight the pivot
    ax_plot.scatter([t[i]], [d[i]], s=140, facecolor="#cc0000",
                     edgecolor="black", linewidth=0.8, zorder=6,
                     label=r"pivot $t_i$")
    ax_plot.scatter([t[i]], [a0], s=80, marker="x", color="black",
                     linewidth=2.0, zorder=7,
                     label=rf"$p(0)\,=\,a_0\,=\,{a0:.0f}$")

    # Annotate weights
    for tk, dk, wk in zip(t, d, w):
        ax_plot.annotate(rf"$w={wk:.2f}$",
                         xy=(tk, dk), xytext=(0, 14),
                         textcoords="offset points",
                         fontsize=8.5, ha="center", color="#444")

    ax_plot.grid(True, alpha=0.3, linewidth=0.5)
    ax_plot.legend(loc="lower right", fontsize=9, frameon=True)

    out = SLIDES / "L_locreg_explainer.png"
    fig.savefig(out, dpi=160, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  saved: {out}")
    print(f"  coefficients: a3={a3:+.4e}  a2={a2:+.4e}  "
          f"a1={a1:+.4e}  a0={a0:+.4f}")


if __name__ == "__main__":
    main()
