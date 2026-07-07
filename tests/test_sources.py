"""Tests for the pluggable trace-source layer (dataio.sources)."""

import pandas as pd
import pytest

from dataio.sources import (
    CANONICAL_COLUMNS,
    empty_canonical,
    ensure_canonical,
    load_trace,
)


def _raw_frame():
    # deliberately unsorted, string times, numeric-typed ids
    return pd.DataFrame({
        "avl_event_time": ["2026-05-05 12:00:02.000", "2026-05-05 12:00:00.000"],
        "latitude": ["41.90", "41.91"],
        "longitude": ["-87.63", "-87.64"],
        "trip_id": [1001350, 1001350],
        "bus_id": [4017, 4017],
        "route_id": [22, 22],
        "pattern_id": [3936, 3936],
    })


def test_ensure_canonical_coerces_types_and_sorts():
    out = ensure_canonical(_raw_frame())
    assert list(out.columns) == CANONICAL_COLUMNS
    assert pd.api.types.is_datetime64_any_dtype(out["avl_event_time"])
    assert out["latitude"].dtype == float
    assert out["trip_id"].tolist() == ["1001350", "1001350"]  # coerced to str
    assert out["avl_event_time"].is_monotonic_increasing      # sorted within trip


def test_ensure_canonical_missing_column_raises():
    with pytest.raises(ValueError, match="missing canonical columns"):
        ensure_canonical(pd.DataFrame({"latitude": [1.0], "longitude": [2.0]}))


def test_empty_canonical_has_all_columns():
    assert list(empty_canonical().columns) == CANONICAL_COLUMNS
    assert len(empty_canonical()) == 0


def test_load_trace_unknown_kind_raises():
    with pytest.raises(ValueError, match="unknown trace source"):
        load_trace({"kind": "does_not_exist"})


def test_phone_csv_source_end_to_end(tmp_path):
    p = tmp_path / "pings_1001350.csv"
    p.write_text(
        "trip_id,bus_id,route_id,pattern_id,avl_event_time,latitude,longitude\n"
        "1001350,4017,22,3936,2026-05-05 12:00:02.000,41.90,-87.63\n"
        "1001350,4017,22,3936,2026-05-05 12:00:00.000,41.91,-87.64\n"
    )
    df = load_trace({"kind": "phone_csv", "path": str(p)})
    assert list(df.columns) == CANONICAL_COLUMNS
    assert len(df) == 2
    assert df["avl_event_time"].is_monotonic_increasing
    assert df["pattern_id"].iloc[0] == "3936"


def test_phone_csv_source_filters_by_route_and_pattern(tmp_path):
    p = tmp_path / "pings.csv"
    p.write_text(
        "trip_id,bus_id,route_id,pattern_id,avl_event_time,latitude,longitude\n"
        "T1,B1,22,3936,2026-05-05 12:00:00.000,41.90,-87.63\n"
        "T2,B2,49,4100,2026-05-05 12:00:00.000,41.95,-87.68\n"
    )
    df = load_trace({"kind": "phone_csv", "path": str(p), "route_id": "22", "pattern_id": "3936"})
    assert df["route_id"].unique().tolist() == ["22"]
    assert len(df) == 1
