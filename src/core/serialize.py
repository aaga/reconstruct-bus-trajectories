"""Compact serialization of LOCREG-PCHIP trajectories.

A PCHIP function is determined by knot times, knot values, and the Hermite
slope at each knot. Storing those three vectors per trip is materially smaller
than storing per-ping smoothed CSV rows, and the function can be rebuilt
exactly with :class:`scipy.interpolate.CubicHermiteSpline` (C¹, identical to
the original PCHIP up to floating point).
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from scipy.interpolate import CubicHermiteSpline

from .pipeline import TripReconstruction


def to_pchip_record(recon: TripReconstruction) -> dict:
    """Extract the (t, x, slope) Hermite triples plus minimal metadata."""
    f = recon.smoothed.f
    t_knots = np.asarray(f.x, dtype=float)
    x_knots = f(t_knots)
    slopes = f.derivative()(t_knots)
    return {
        "trip_id": recon.meta.trip_id,
        "bus_id": recon.meta.bus_id,
        "route_id": recon.meta.route_id,
        "pattern_id": recon.meta.pattern_id,
        "first_ping_iso": recon.meta.first_ping.isoformat(),
        "last_ping_iso": recon.meta.last_ping.isoformat(),
        "n_pings_raw": recon.meta.n_pings,
        "n_pings_on_route": recon.meta.n_on_route,
        "t_knots": t_knots.tolist(),
        "x_knots": x_knots.tolist(),
        "slopes": slopes.tolist(),
    }


def from_pchip_record(record: dict) -> CubicHermiteSpline:
    """Rebuild a function ``f(t) -> distance_m`` from a record."""
    t = np.asarray(record["t_knots"], dtype=float)
    x = np.asarray(record["x_knots"], dtype=float)
    m = np.asarray(record["slopes"], dtype=float)
    return CubicHermiteSpline(t, x, m, extrapolate=False)


def save_records(records: list[dict], out_path: str | Path) -> None:
    """Write a JSON bundle of trip records."""
    Path(out_path).write_text(json.dumps({"trips": records}, separators=(",", ":")))


def load_records(in_path: str | Path) -> list[dict]:
    return json.loads(Path(in_path).read_text())["trips"]


def save_records_npz(records: list[dict], out_path: str | Path) -> None:
    """Write trip records as a single ``.npz`` (smallest disk footprint)."""
    arrays: dict[str, np.ndarray] = {}
    meta = []
    for i, r in enumerate(records):
        arrays[f"t_{i}"] = np.asarray(r["t_knots"], dtype=np.float32)
        arrays[f"x_{i}"] = np.asarray(r["x_knots"], dtype=np.float32)
        arrays[f"m_{i}"] = np.asarray(r["slopes"], dtype=np.float32)
        meta.append(
            {
                "i": i,
                "trip_id": r["trip_id"],
                "bus_id": r["bus_id"],
                "route_id": r["route_id"],
                "pattern_id": r["pattern_id"],
                "first_ping_iso": r["first_ping_iso"],
                "last_ping_iso": r["last_ping_iso"],
                "n_pings_raw": r["n_pings_raw"],
                "n_pings_on_route": r["n_pings_on_route"],
            }
        )
    arrays["__meta__"] = np.array(json.dumps(meta), dtype=object)
    np.savez_compressed(out_path, **arrays)
