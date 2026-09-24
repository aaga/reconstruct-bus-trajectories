"""Re-source each recorded trip's LOW-FREQ layer from the AVL archive.

The obs bundles were built from the R2 GTFS-rt scrape, which carries
position only — so the low-freq trajectory could never use VCHIP-ME, and
the single-trip view disagreed with the network pipeline about the same
bus on the same day (20 s AVL + VCHIP-ME there, 30 s R2 + LOCREG here).

Rebuilding the bundles outright goes through the observation webapp
(bus-observation-tool.pages.dev) and re-derives everything; when only the
low-freq layer needs changing, this rewrites the ``r2`` source in place:
archive pings for the vehicle's ride window, clustered to the ride,
map-matched to the same shape, reconstructed with fit_trajectory.
Everything else in the bundle — observed events, features, phone layer —
is left untouched.

    PYTHONPATH=src uv run python analysis/relow_freq_archive.py
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from analysis.comparison import dense_grid, ride_cluster  # noqa: E402
from core.mapmatch.shape_snap import SnapToShapeMatcher  # noqa: E402
from core.reconstruct import reconstruct_trip  # noqa: E402
from dataio.realtime import trip_archive_pings  # noqa: E402

OBS = REPO / "outputs" / "obs_trips"


def redo(path: Path, pad_b: int = 90, pad_a: int = 30) -> str:
    obs = json.loads(path.read_text())
    bus = str(obs.get("bus_id") or "")
    shape = obs["shape"]
    poly = np.asarray(shape["polyline_lonlat"], dtype=float)  # lon, lat
    latlon = np.column_stack([poly[:, 1], poly[:, 0]])
    cum = np.asarray(shape["cumdist_m"], dtype=float)

    # ride window: the phone layer's span, in epoch ms
    t0 = obs["t0_epoch_ms"]
    pt = obs["phone"]["curve"]["t"]
    start_ms = int(t0 + pt[0] * 1000)
    end_ms = int(t0 + pt[-1] * 1000)

    raw = trip_archive_pings(bus, start_ms, end_ms)
    if raw.empty:
        return f"{path.stem}: no archive pings for bus {bus}"
    rc = ride_cluster(raw, start_ms, end_ms, pad_b, pad_a)
    if rc.empty or len(rc) < 10:
        return f"{path.stem}: ride cluster too small ({len(rc)})"

    matcher = SnapToShapeMatcher(latlon, max_perp_m=50.0,
                                 dist_along_m_per_vertex=cum)
    rc = rc.copy()
    rc["avl_event_time"] = rc["avl_event_time"].astype("datetime64[ms]")
    rc["pattern_id"] = str(obs.get("pattern_id") or "")
    try:
        recon = reconstruct_trip(rc, matcher, bandwidth=5)
    except ValueError as e:
        return f"{path.stem}: reconstruct failed ({e})"

    offset_s = (int(rc["epoch_ms"].iloc[0]) - t0) / 1000.0
    tg, xg, vg = dense_grid(recon)
    obs["r2"] = {
        "anchor_offset_s": round(offset_s, 1),
        "curve": {
            "t": [round(offset_s + float(v), 1) for v in tg],
            "dist_m": [round(float(v), 1) for v in xg],
            "speed_mph": [round(float(v), 2) for v in vg],
        },
        "raw_pings": [
            {"t": round(offset_s + float(t), 1), "dist_m": round(float(d), 1)}
            for t, d in zip(recon.t, recon.d)
        ],
        # The proximity decomposition is no longer consumed (the speed tab
        # classifies from door overlap now), so the block is left empty
        # rather than recomputed.
        "delays": [],
        "n_pings": int(recon.meta.n_pings),
        "n_on_route": int(recon.meta.n_on_route),
        "source": "avl_archive",
        "smoother": type(recon.smoothed.f).__name__,
    }
    path.write_text(json.dumps(obs))
    v = rc["speed_mps"].to_numpy(dtype=float) if "speed_mps" in rc else np.array([])
    gaps = np.diff(rc["epoch_ms"].to_numpy()) / 1000.0
    return (f"{path.stem}: {len(rc)} pings, median gap "
            f"{np.median(gaps):.0f}s, speeds {np.isfinite(v).mean():.0%}, "
            f"{obs['r2']['smoother']}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--keys", nargs="*")
    a = ap.parse_args()
    files = ([OBS / f"{k}.json" for k in a.keys] if a.keys
             else sorted(p for p in OBS.glob("*.json") if p.stem != "index"))
    for f in files:
        if not f.exists():
            print(f"  {f.stem}: missing")
            continue
        print("  " + redo(f), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
