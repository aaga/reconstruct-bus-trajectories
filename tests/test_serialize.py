"""Round-trip tests for compact PCHIP serialization."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from core.serialize import (
    from_pchip_record,
    load_records,
    save_records,
    to_pchip_record,
)
from core.smooth import locreg_pchip


class _FakeRecon:
    """Minimal stand-in for TripReconstruction (only attributes serialize uses)."""

    class _Meta:
        trip_id = "999"
        bus_id = "1234"
        route_id = "22"
        pattern_id = "3936"
        n_pings = 100
        n_on_route = 100
        first_ping = __import__("pandas").Timestamp("2026-03-31 09:00:00")
        last_ping = __import__("pandas").Timestamp("2026-03-31 10:30:00")

    def __init__(self, smoothed):
        self.smoothed = smoothed
        self.meta = self._Meta()


def test_pchip_record_round_trip(tmp_path: Path):
    rng = np.random.default_rng(2)
    t = np.linspace(0, 200, 150)
    d = np.cumsum(np.abs(rng.normal(2.0, 0.4, size=t.size)))
    res = locreg_pchip(t, d, bandwidth=20, degree=3)

    recon = _FakeRecon(res)
    rec = to_pchip_record(recon)
    f_rebuilt = from_pchip_record(rec)
    grid = np.linspace(t[0], t[-1], 500)
    np.testing.assert_allclose(res.f(grid), f_rebuilt(grid), atol=1e-9)


def test_save_and_load_records(tmp_path: Path):
    rng = np.random.default_rng(3)
    records = []
    for k in range(2):
        t = np.linspace(0, 100, 80)
        d = np.cumsum(np.abs(rng.normal(2.0, 0.4, size=t.size)))
        res = locreg_pchip(t, d, bandwidth=20, degree=3)
        records.append(to_pchip_record(_FakeRecon(res)))
    p = tmp_path / "out.json"
    save_records(records, p)
    loaded = load_records(p)
    assert len(loaded) == 2
    json.dumps(loaded)  # serializable


def test_compact_record_smaller_than_per_ping_csv(tmp_path: Path):
    """Compact record should be much smaller than the equivalent per-ping CSV."""
    rng = np.random.default_rng(4)
    t = np.linspace(0, 6000, 320)  # ~100 min, 320 pings
    d = np.cumsum(np.abs(rng.normal(60.0, 10.0, size=t.size)))
    res = locreg_pchip(t, d, bandwidth=20, degree=3)
    rec = to_pchip_record(_FakeRecon(res))
    p = tmp_path / "one.json"
    save_records([rec], p)

    # Per-ping CSV emitted by CLI is ~7 floats × 320 rows ≈ 30+ KB.
    # The compact record should be on the order of 10 KB (3 floats × 320 + meta).
    compact_size = p.stat().st_size
    assert compact_size < 20_000, f"compact record larger than expected: {compact_size}"
