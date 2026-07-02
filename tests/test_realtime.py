"""Unit tests for the shared R2 client's pure transform.

Only ``to_avl_csv_format`` is exercised here — the fetch/manifest helpers hit
the network and are covered by the scripts that drive them. ``to_avl_csv_format``
is the one piece of pure logic, and it's the contract the reconstruct CLI reads.
"""

from __future__ import annotations

import pandas as pd

from dataio.gtfs import load_avl_csv
from dataio import realtime
from dataio.realtime import to_avl_csv_format, trip_avl_pings


def _r2_pings():
    """A tiny R2-archive-shaped frame (the columns to_avl_csv_format reads)."""
    ts = pd.to_datetime(
        ["2026-05-05 12:00:00", "2026-05-05 12:00:30"], utc=True
    )
    return pd.DataFrame({
        "entity_id": ["e1", "e2"],
        "vehicle_id": ["4017", "4017"],
        "route_id": ["22", "22"],
        "trip_id": ["1001350", "1001350"],
        "timestamp": ts,
        "latitude": [42.019, 42.015],
        "longitude": [-87.672, -87.671],
        "bearing": [180.0, None],
    })


def test_to_avl_csv_format_roundtrips_through_load_avl_csv(tmp_path):
    out = to_avl_csv_format(_r2_pings(), tmp_path / "pings.csv", pattern_id="3936")
    assert out.exists()

    df = load_avl_csv(out, drop_deadhead=False)
    assert list(df.trip_id.unique()) == ["1001350"]
    assert set(df.bus_id) == {"4017"}
    assert (df.pattern_id == "3936").all()
    assert (df.route_id == "22").all()
    # latitude/longitude survive as floats in the right place
    assert df.latitude.iloc[0] == 42.019


def test_to_avl_csv_format_writes_canonical_columns(tmp_path):
    out = to_avl_csv_format(_r2_pings(), tmp_path / "p.csv")
    cols = pd.read_csv(out, nrows=0).columns.tolist()
    # A representative slice of the 25-column canonical AVL header.
    for c in ("bus_id", "avl_event_time", "route_id", "pattern_id",
              "trip_id", "latitude", "longitude", "heading"):
        assert c in cols


def test_to_avl_csv_format_event_time_is_naive_microsecond(tmp_path):
    out = to_avl_csv_format(_r2_pings(), tmp_path / "p.csv")
    raw = pd.read_csv(out, dtype=str)
    # tz dropped, microsecond precision retained, matches load_avl_csv's format
    assert raw.avl_event_time.iloc[0] == "2026-05-05 12:00:00.000000"


def test_trip_avl_pings_filters_by_trip_and_reshapes(monkeypatch):
    """trip_avl_pings selects one trip across the spanned hours (no network)."""
    pings = _r2_pings()
    other = pings.copy()
    other["trip_id"] = "9999999"  # a different trip in the same hour-file
    hour_frame = pd.concat([pings, other], ignore_index=True)
    # Stub the network fetch: any requested hour returns our synthetic frame.
    monkeypatch.setattr(realtime, "load_hour", lambda path, cache_dir=None: hour_frame)

    start = pd.Timestamp("2026-05-05 12:00:00", tz="UTC")
    manifest = pd.DataFrame({
        "agency": ["cta"], "year": [start.year], "month": [start.month],
        "day": [start.day], "hour": [start.hour],
        "path": ["agency=cta__year=2026__month=05__day=05__hour=12.parquet"],
    })
    start_ms = int(start.value // 1_000_000)
    end_ms = int((start + pd.Timedelta(seconds=30)).value // 1_000_000)

    out = trip_avl_pings("22", "4017", "1001350", start_ms, end_ms, manifest=manifest)

    assert list(out.trip_id.unique()) == ["1001350"]  # other trip filtered out
    assert list(out.columns) == [
        "trip_id", "bus_id", "route_id", "avl_event_time",
        "latitude", "longitude", "heading", "epoch_ms",
    ]
    assert len(out) == 2
    assert out.bus_id.iloc[0] == "4017"
    assert out.avl_event_time.iloc[0] == "2026-05-05 12:00:00.000000"
    assert out.epoch_ms.iloc[0] == start_ms
    assert out.latitude.iloc[0] == 42.019


def test_trip_avl_pings_missing_trip_returns_empty(monkeypatch):
    monkeypatch.setattr(realtime, "load_hour", lambda path, cache_dir=None: _r2_pings())
    start = pd.Timestamp("2026-05-05 12:00:00", tz="UTC")
    manifest = pd.DataFrame({
        "agency": ["cta"], "year": [start.year], "month": [start.month],
        "day": [start.day], "hour": [start.hour], "path": ["p.parquet"],
    })
    ms = int(start.value // 1_000_000)
    out = trip_avl_pings("22", "4017", "0000000", ms, ms, manifest=manifest)
    assert out.empty
