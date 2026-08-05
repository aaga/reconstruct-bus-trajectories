"""Classify each (segment, shape) by its movement through the DOWNSTREAM
intersection: THRU, LEFT, RIGHT — or ENDS when the shape terminates there.

Method: from the GTFS shape polyline, take the approach bearing over the
last {WINDOW_M} m before the segment's downstream boundary and the exit
bearing over the first {WINDOW_M} m after it. Compass delta (wrapped to
±180°) classifies:  |Δ| < {THRU_DEG}° → T, Δ > 0 → R, Δ < 0 → L.
Shapes with < {WINDOW_M} m remaining after the boundary → E.

Output: outputs/network/<city>/movements.json
    {seg_id: {shape_id: "T"|"L"|"R"|"E"}}

Consumed by build_distributions (per-movement distribution split) and the
dashboard's movement filter. Pure annotation — touches no metrics.

Usage:
    PYTHONPATH=src uv run python analysis/network/turn_movements.py --city cta
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from dataio.cities import get_city  # noqa: E402
from dataio.gtfs import load_gtfs_shape_with_dist  # noqa: E402

WINDOW_M = 40.0   # bearing window on each side of the boundary
THRU_DEG = 30.0   # |delta| below this = straight through


def _bearing(lat1, lon1, lat2, lon2) -> float:
    """Compass bearing (deg, 0=N, clockwise) via local equirectangular."""
    coslat = np.cos(np.radians((lat1 + lat2) / 2))
    dx = (lon2 - lon1) * coslat
    dy = lat2 - lat1
    return float(np.degrees(np.arctan2(dx, dy)))


def classify(dist_m: np.ndarray, lats: np.ndarray, lons: np.ndarray,
             x_end: float) -> str:
    total = float(dist_m[-1])
    if x_end > total - WINDOW_M:
        return "E"
    at = lambda x: (float(np.interp(x, dist_m, lats)),
                    float(np.interp(x, dist_m, lons)))
    a0 = at(max(0.0, x_end - WINDOW_M)); a1 = at(x_end)
    b1 = at(min(total, x_end + WINDOW_M))
    b_in = _bearing(*a0, *a1)
    b_out = _bearing(*a1, *b1)
    delta = (b_out - b_in + 180) % 360 - 180
    if abs(delta) < THRU_DEG:
        return "T"
    return "R" if delta > 0 else "L"


def build(city_id: str) -> None:
    city = get_city(city_id)
    base = REPO / "outputs" / "network" / city.city_id
    reg = json.loads((base / "segment_registry.json").read_text())
    gtfs = city.resolve(city.gtfs_zip)

    out: dict[str, dict[str, str]] = {}
    tally: Counter = Counter()
    for shape_id, rec in reg["shapes"].items():
        polyline, dist_m = load_gtfs_shape_with_dist(gtfs, shape_id)
        pts = np.asarray(polyline, dtype=float)
        lats, lons = pts[:, 0], pts[:, 1]
        dist_m = np.asarray(dist_m, dtype=float)
        for seg_id, _x_lo, x_hi in rec["seg_bounds"]:
            mv = classify(dist_m, lats, lons, float(x_hi))
            out.setdefault(seg_id, {})[shape_id] = mv
            tally[mv] += 1

    path = base / "movements.json"
    path.write_text(json.dumps(out))
    multi = sum(1 for m in out.values() if len(set(m.values())) > 1)
    print(f"wrote {path}: {len(out)} segments, "
          f"{sum(len(m) for m in out.values())} (seg, shape) pairs "
          f"{dict(tally)}; segments with mixed movements: {multi}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--city", default="cta")
    args = ap.parse_args()
    build(args.city)


if __name__ == "__main__":
    main()
