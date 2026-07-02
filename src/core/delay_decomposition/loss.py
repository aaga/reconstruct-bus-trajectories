"""Accel/decel shoulder time for each slowdown event.

For each detected event, find the deceleration shoulder before ``t_start``
(time spent dropping from a cruise threshold down to the event boundary) and
the acceleration shoulder after ``t_end`` (back up to cruise). Their sum is
the event's ``loss_s`` — the time spent transitioning rather than dwelling /
queuing per se.
"""

from __future__ import annotations

import numpy as np

from .events import Event

M_PER_S_TO_MPH = 2.236936292054402


def loss_shoulders_for_event(
    t_dense: np.ndarray,
    v_mph_dense: np.ndarray,
    event: Event,
    *,
    cruise_threshold_mph: float = 12.0,
) -> tuple[tuple[float, float], tuple[float, float]]:
    """Return (decel_shoulder, accel_shoulder) as (t_lo, t_hi) intervals.

    Each interval is the wall-clock window during which the bus is in
    accel/decel transition for this event (between the cruise threshold
    and the event boundary). Empty intervals are returned as (t, t).
    """
    if t_dense.size == 0:
        return ((event.t_start, event.t_start), (event.t_end, event.t_end))

    i_start = int(np.searchsorted(t_dense, event.t_start, side="left"))
    i_end = int(np.searchsorted(t_dense, event.t_end, side="right")) - 1
    i_start = max(0, min(i_start, len(t_dense) - 1))
    i_end = max(0, min(i_end, len(t_dense) - 1))

    decel_lo = event.t_start
    if i_start > 0:
        k = i_start - 1
        while k >= 0 and v_mph_dense[k] < cruise_threshold_mph:
            if v_mph_dense[k] <= v_mph_dense[k + 1]:
                break
            k -= 1
        if k + 1 < i_start:
            decel_lo = float(t_dense[k + 1])

    accel_hi = event.t_end
    if i_end < len(t_dense) - 1:
        k = i_end + 1
        while k < len(t_dense) and v_mph_dense[k] < cruise_threshold_mph:
            if v_mph_dense[k] <= v_mph_dense[k - 1]:
                break
            k += 1
        if k - 1 > i_end:
            accel_hi = float(t_dense[k - 1])

    return ((decel_lo, event.t_start), (event.t_end, accel_hi))


def loss_time_for_event(
    t_dense: np.ndarray,
    v_mph_dense: np.ndarray,
    event: Event,
    *,
    cruise_threshold_mph: float = 12.0,
) -> float:
    """Return decel-before + accel-after shoulder duration in seconds.

    Convenience wrapper around :func:`loss_shoulders_for_event`. Use the
    interval form when you need to clip the shoulders to segment time
    bounds; use this scalar form for quick diagnostics.
    """
    decel, accel = loss_shoulders_for_event(
        t_dense, v_mph_dense, event,
        cruise_threshold_mph=cruise_threshold_mph,
    )
    return (decel[1] - decel[0]) + (accel[1] - accel[0])
