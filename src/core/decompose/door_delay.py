"""Door-aware delay classification — the temporal rules, shared.

These are exactly the rules ``analysis/network/delay_events.py`` applies to
build the network distributions, factored out so the single-trip speed view
can run them over the phone (high-freq) and R2 (low-freq) curves and cannot
drift from what the network tab shows:

    slow event      speed < THRESHOLD_MPH sustained >= MIN_EVENT_S
    no door overlap                          -> "nd"    (red)
    overlap, > PORTION_MIN_S before 1st open -> "pre"   (teal)
    overlap, > PORTION_MIN_S after 1st close -> "post"  (purple)
      ...with >= 2 cycles swallowed          -> "post2"
    every door cycle, unioned with any overlapping event -> "dw" (blue)

Overlap is the half-open test ``close > event_start AND open < event_end``,
so a cycle straddling either end still counts; the straddled side simply
yields no shoulder.

``post``/``post2`` attributed to a NEAR-SIDE stop are reported with
``near_side=True`` — the caller renders those as the purple-red combo,
because delay after a near-side stop is a stop-then-signal compound rather
than pure stop loss.

What is deliberately NOT here: the network pipeline's spatial guards (a
shoulder keeps its class only when its trajectory segment matches its
door's raw segment; dwell blobs are cut at segment boundaries). Those need
a segment frame the single-trip view doesn't have. ``tests/test_door_delay``
pins the temporal outputs of the two implementations together.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .events import AbsoluteSpeedThreshold, detect_events

THRESHOLD_MPH = 5.0
MIN_EVENT_S = 15.0
PORTION_MIN_S = 10.0
DENSE_DT_S = 2.0


@dataclass(frozen=True)
class Piece:
    """One classified stretch of trip time."""

    cls: str            # nd | pre | post | post2 | dw
    t_start: float
    t_end: float
    stop_id: str | None = None
    stop_name: str | None = None
    near_side: bool = False

    @property
    def duration_s(self) -> float:
        return self.t_end - self.t_start

    @property
    def render_cls(self) -> str:
        """Class the UI draws: near-side post gets its own combo shading."""
        if self.near_side and self.cls in ("post", "post2"):
            return f"{self.cls}_ns"
        return self.cls


def classify(
    t: np.ndarray,
    x: np.ndarray,
    doors: np.ndarray,
    *,
    stop_ids: list | None = None,
    stop_names: dict | None = None,
    near_side: set | None = None,
    threshold_mph: float = THRESHOLD_MPH,
    min_event_s: float = MIN_EVENT_S,
    portion_min_s: float = PORTION_MIN_S,
    emit_short_shoulders: bool = False,
) -> list[Piece]:
    """Classify one trip's time line.

    ``t``/``x`` are a monotone trajectory (seconds, metres) in any consistent
    time base; ``doors`` is ``[[open, close], ...]`` in that same base.
    ``stop_ids[i]`` is the stop attributed to door cycle ``i``.

    ``emit_short_shoulders`` adds the slow time adjacent to a door that
    misses ``portion_min_s`` back as plain "nd". The network pipeline drops
    it (it is neither stop loss nor a standalone delay), but the speed view
    needs it so the dwell union renders as ONE continuous bar rather than a
    striped one with holes.
    """
    t = np.asarray(t, dtype=float)
    x = np.asarray(x, dtype=float)
    if t.size < 4:
        return []
    doors = (np.asarray(doors, dtype=float).reshape(-1, 2)
             if doors is not None and len(doors) else np.empty((0, 2)))
    stop_ids = list(stop_ids or [None] * len(doors))
    stop_names = stop_names or {}
    near_side = near_side or set()

    # Dense, monotone grid — the frame detect_events expects.
    tg = np.arange(float(t[0]), float(t[-1]), DENSE_DT_S)
    if tg.size < 4:
        return []
    xg = np.maximum.accumulate(np.interp(tg, t, x))
    vg = np.gradient(xg, tg) * 2.23694
    events = detect_events(tg, xg, vg, AbsoluteSpeedThreshold(threshold_mph),
                           min_duration_s=min_event_s)

    def _stop(k: int):
        sid = stop_ids[k] if k < len(stop_ids) else None
        sid = None if sid is None else str(sid)
        return sid, stop_names.get(sid), (sid in near_side)

    out: list[Piece] = []
    for ev in events:
        a, b = float(ev.t_start), float(ev.t_end)
        oidx = (np.where((doors[:, 1] > a) & (doors[:, 0] < b))[0]
                if len(doors) else np.empty(0, dtype=int))
        if len(oidx) == 0:
            out.append(Piece("nd", a, b))
            continue
        overl = doors[oidx]
        open_min = float(overl[:, 0].min())
        k_open = int(oidx[int(np.argmin(overl[:, 0]))])
        close_first = float(overl[:, 1].min())
        k_close = int(oidx[int(np.argmin(overl[:, 1]))])
        if open_min - a > portion_min_s:
            sid, nm, ns = _stop(k_open)
            out.append(Piece("pre", a, open_min, sid, nm, ns))
        elif emit_short_shoulders and open_min > a:
            out.append(Piece("nd", a, open_min))
        if b - close_first > portion_min_s:
            cls = "post2" if len(oidx) > 1 else "post"
            sid, nm, ns = _stop(k_close)
            out.append(Piece(cls, close_first, b, sid, nm, ns))
        elif emit_short_shoulders and b > close_first:
            out.append(Piece("nd", close_first, b))

    # Dwell: every door cycle, merged with any event it overlaps, so nothing
    # is double counted. Quick stops with no 15 s event still contribute.
    if len(doors):
        pieces = [(float(o), float(c)) for o, c in doors]
        for ev in events:
            a, b = float(ev.t_start), float(ev.t_end)
            if ((doors[:, 1] > a) & (doors[:, 0] < b)).any():
                pieces.append((a, b))
        pieces.sort()
        blobs: list[list[float]] = []
        for lo, hi in pieces:
            if blobs and lo <= blobs[-1][1]:
                blobs[-1][1] = max(blobs[-1][1], hi)
            else:
                blobs.append([lo, hi])
        for lo, hi in blobs:
            kk = [k for k in range(len(doors))
                  if doors[k, 0] <= hi and doors[k, 1] >= lo]
            if not kk:
                continue
            sid, nm, _ns = _stop(kk[0])
            out.append(Piece("dw", lo, hi, sid, nm, False))

    out.sort(key=lambda p: (p.t_start, p.cls))
    return out
