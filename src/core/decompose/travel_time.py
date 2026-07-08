"""Segment observed and free-flow travel times.

`segment_observed_time` uses the chapter Eq 3.3 convention: the *last* time
``f(t) = x`` at the segment endpoint (so dwell at the downstream signal is
attributed to the segment that ends at it). `segment_freeflow_table` gathers
per-segment T_obs across a directory of late-night reconstructions and
returns the p5 (95th-percentile-fastest) per segment.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from ..serialize import from_pchip_record, load_records
from .segments import Segment


def _last_t_at_x(f, x_target: float) -> float:
    """Return max{t in [f.x[0], f.x[-1]] : f(t) == x_target}.

    `f` is monotonic non-decreasing (LOCREG-PCHIP property), so the set is
    either a single point or an interval [t_a, t_d] (when the bus dwelled
    at distance x_target). We use the right endpoint, found via a dense
    inverse search + linear interpolation at the boundary.
    """
    t_lo, t_hi = float(f.x[0]), float(f.x[-1])
    # Dense grid for a robust max search.
    n = 4000
    ts = np.linspace(t_lo, t_hi, n)
    xs = np.asarray(f(ts))
    # Find indices where x crosses x_target. We want the last t where x >= x_target
    # starts being true (i.e. the last crossing from below to above, or, in dwell
    # cases, the very end of the at-x_target interval).
    above = xs >= x_target
    if not above.any():
        return t_hi  # bus never reached x_target — clip to end
    if above.all():
        return t_lo  # already past it at t_lo — clip to start
    # Largest index where xs[i-1] < x_target <= xs[i] OR xs is in dwell at x_target.
    # The "last time at x" corresponds to the latest i such that xs[i] <= x_target + eps
    # AND xs[i+1] > x_target + eps. Equivalent: largest i with xs[i] <= x_target.
    eps = 1e-6
    le = xs <= x_target + eps
    if not le.any():
        return t_lo
    i = int(np.where(le)[0][-1])
    if i == n - 1:
        return float(ts[-1])
    # Linear interp between i and i+1 to find where x = x_target.
    x0, x1 = float(xs[i]), float(xs[i + 1])
    if x1 == x0:
        return float(ts[i])
    frac = (x_target - x0) / (x1 - x0)
    frac = max(0.0, min(1.0, frac))
    return float(ts[i] + frac * (ts[i + 1] - ts[i]))


def segment_observed_time(f, segment: Segment) -> float:
    """T_obs = (last time at x_end) - (last time at x_start).

    Using the "last time" convention attributes dwell at each signal to the
    segment that ends there, per chapter Eq 3.3.
    """
    t_end = _last_t_at_x(f, segment.x_end_m)
    t_start = _last_t_at_x(f, segment.x_start_m)
    return t_end - t_start


def segment_observed_time_for_record(record: dict, segment: Segment) -> float:
    f = from_pchip_record(record)
    return segment_observed_time(f, segment)


def per_segment_observed_times(
    trajectories_json: Path, segments: list[Segment]
) -> dict[str, list[float]]:
    """For each segment, return a list of T_obs across every trip in the JSON."""
    records = load_records(trajectories_json)
    out: dict[str, list[float]] = {s.seg_id: [] for s in segments}
    for rec in records:
        f = from_pchip_record(rec)
        for s in segments:
            try:
                t_obs = segment_observed_time(f, s)
            except Exception:
                continue
            if t_obs > 0:
                out[s.seg_id].append(t_obs)
    return out


def segment_freeflow_table(
    trajectories_json: Path,
    segments: list[Segment],
    *,
    percentile: int = 5,
) -> dict[str, float]:
    """For each segment, return the p_th percentile of T_obs across all trips
    in `trajectories_json` (default p=5, i.e. 95th-percentile-fastest)."""
    per_seg = per_segment_observed_times(trajectories_json, segments)
    out: dict[str, float] = {}
    for seg_id, samples in per_seg.items():
        if not samples:
            continue
        out[seg_id] = float(np.percentile(samples, percentile))
    return out


def save_freeflow_table(table: dict[str, float], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(table, indent=2))


def load_freeflow_table(in_path: Path) -> dict[str, float]:
    return {k: float(v) for k, v in json.loads(Path(in_path).read_text()).items()}
