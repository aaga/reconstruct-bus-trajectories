"""Slowdown event detection on a dense (t, x, v) grid.

An event is a contiguous interval where a pluggable threshold function flags
the bus as "slow." The default is the chapter-style flat-detection
:class:`AbsoluteSpeedThreshold` (v < 5 mph), but the protocol leaves room for
a future free-flow-ratio threshold without touching the rest of the pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import numpy as np


@runtime_checkable
class EventThreshold(Protocol):
    """Returns a boolean mask flagging each sample as slow (True) or not."""

    def __call__(
        self, t: np.ndarray, x: np.ndarray, v_mph: np.ndarray
    ) -> np.ndarray: ...


@dataclass(frozen=True)
class AbsoluteSpeedThreshold:
    threshold_mph: float = 5.0

    def __call__(self, t, x, v_mph):
        return v_mph < self.threshold_mph


@dataclass(frozen=True)
class Event:
    t_start: float
    t_end: float
    x_start: float
    x_end: float
    min_v_mph: float

    @property
    def duration_s(self) -> float:
        return self.t_end - self.t_start


def _runs_true(mask: np.ndarray) -> list[tuple[int, int]]:
    """Return (i_lo, i_hi_inclusive) index pairs of contiguous True runs."""
    if not mask.any():
        return []
    diffs = np.diff(mask.astype(np.int8))
    starts = list(np.where(diffs == 1)[0] + 1)
    ends = list(np.where(diffs == -1)[0])
    if mask[0]:
        starts.insert(0, 0)
    if mask[-1]:
        ends.append(len(mask) - 1)
    return list(zip(starts, ends))


def detect_events(
    t: np.ndarray,
    x: np.ndarray,
    v_mph: np.ndarray,
    threshold: EventThreshold,
    *,
    min_duration_s: float = 15.0,
    creep_merge_m: float = 20.0,
) -> list[Event]:
    """Detect slowdown events on the dense grid; merge close ones (creeping)."""
    mask = threshold(t, x, v_mph)
    runs = _runs_true(mask)
    if not runs:
        return []

    raw: list[Event] = []
    for i_lo, i_hi in runs:
        ev = Event(
            t_start=float(t[i_lo]),
            t_end=float(t[i_hi]),
            x_start=float(x[i_lo]),
            x_end=float(x[i_hi]),
            min_v_mph=float(v_mph[i_lo : i_hi + 1].min()),
        )
        raw.append(ev)

    # Drop events shorter than min_duration_s.
    raw = [e for e in raw if e.duration_s >= min_duration_s]
    if not raw:
        return []

    # Merge events whose distance gap to the next is below creep_merge_m
    # (matches chapter §3.3.5's "creeping behavior" treatment).
    merged: list[Event] = [raw[0]]
    for ev in raw[1:]:
        last = merged[-1]
        gap_m = ev.x_start - last.x_end
        if gap_m < creep_merge_m:
            merged[-1] = Event(
                t_start=last.t_start,
                t_end=ev.t_end,
                x_start=last.x_start,
                x_end=ev.x_end,
                min_v_mph=min(last.min_v_mph, ev.min_v_mph),
            )
        else:
            merged.append(ev)
    return merged
