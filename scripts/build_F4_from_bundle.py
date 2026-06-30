"""Render F4_timespace_alltrips_aligned.png from the already-reconstructed
``out_r2_bw5/trajectories.json`` bundle, skipping the slow re-reconstruction
that the original ``build_alltrip_aligned.py`` performs.

Equivalent figure, faster: reads the serialized PCHIP records, wraps each
in a minimal shim that matches the ``ReconstructedTrip`` shape that
``plot_aligned`` expects (``.t`` array + ``.smoothed.f`` spline), and
delegates to the original plotter.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from scipy.interpolate import CubicHermiteSpline

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "scripts"))

from bus_trajectories.serialize import load_records  # noqa: E402
from build_alltrip_aligned import plot_aligned  # noqa: E402

BUNDLE = REPO / "out_r2_bw5" / "trajectories.json"
OUT = REPO / "figures" / "F4_timespace_alltrips_aligned.png"
MAX_DURATION_MIN = 120.0  # F4-only filter; keeps the figure readable


class _Smoothed:
    def __init__(self, f):
        self.f = f


class _ShimRecon:
    def __init__(self, t_knots, x_knots, slopes):
        f = CubicHermiteSpline(t_knots, x_knots, slopes, extrapolate=False)
        self.t = np.asarray(t_knots, dtype=float)
        self.smoothed = _Smoothed(f)


def main() -> int:
    print(f"Loading bundle: {BUNDLE}")
    records = list(load_records(BUNDLE))
    print(f"Loaded {len(records)} records.")
    max_dur_s = MAX_DURATION_MIN * 60
    kept = [
        r for r in records
        if (r["t_knots"][-1] - r["t_knots"][0]) <= max_dur_s
    ]
    n_drop = len(records) - len(kept)
    print(f"Filtered out {n_drop} trip(s) longer than {MAX_DURATION_MIN:.0f} min; "
          f"keeping {len(kept)} for F4.")
    recons = {
        r["trip_id"]: _ShimRecon(r["t_knots"], r["x_knots"], r["slopes"])
        for r in kept
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    plot_aligned(recons, OUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
