"""Unit tests for the GTFS / AVL-CSV loaders in dataio.gtfs.

These are pure parsers (no network); they were previously untested. Covers the
deadhead drop and per-trip time sort in ``load_avl_csv``, the feet->meter
conversion of ``shape_dist_traveled`` in ``load_gtfs_shape_with_dist``, and the
CTA pattern->shape id rule (including the five-digit pad fix).
"""

from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path

import numpy as np

from dataio.gtfs import (
    load_avl_csv,
    load_gtfs_shape_with_dist,
    shape_id_for_pattern,
)

_AVL_HEADER = "bus_id,avl_event_time,route_id,pattern_id,trip_id,latitude,longitude\n"


def _write_avl(path: Path, rows: list[str]) -> Path:
    path.write_text(_AVL_HEADER + "".join(r + "\n" for r in rows))
    return path


def test_load_avl_csv_drops_deadhead_and_sorts_within_trip(tmp_path):
    # trip A out of order; one deadhead (route 992) row that must be dropped.
    rows = [
        "4017,2026-05-05 12:00:30.000000,22,3936,A,42.01,-87.67",
        "4017,2026-05-05 12:00:00.000000,22,3936,A,42.02,-87.67",
        "4017,2026-05-05 12:00:10.000000,992,0,DH,42.00,-87.60",
    ]
    df = load_avl_csv(_write_avl(tmp_path / "avl.csv", rows))
    assert "992" not in set(df.route_id)          # deadhead dropped
    a = df[df.trip_id == "A"]
    assert list(a.avl_event_time.dt.second) == [0, 30]  # sorted ascending
    assert a.latitude.tolist() == [42.02, 42.01]   # floats, follow the sort


def test_load_avl_csv_keeps_deadhead_when_requested(tmp_path):
    rows = ["4017,2026-05-05 12:00:10.000000,992,0,DH,42.00,-87.60"]
    df = load_avl_csv(_write_avl(tmp_path / "avl.csv", rows), drop_deadhead=False)
    assert set(df.route_id) == {"992"}


def _gtfs_zip_with_shape(path: Path, sid: str, pts) -> Path:
    """Write a minimal GTFS zip containing only shapes.txt."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["shape_id", "shape_pt_sequence", "shape_pt_lat",
                "shape_pt_lon", "shape_dist_traveled"])
    for seq, lat, lon, dist_ft in pts:
        w.writerow([sid, seq, lat, lon, dist_ft])
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("shapes.txt", buf.getvalue())
    return path


def test_load_gtfs_shape_with_dist_converts_feet_to_meters(tmp_path):
    # CTA stores shape_dist_traveled in feet; loader returns meters.
    z = _gtfs_zip_with_shape(
        tmp_path / "gtfs.zip", "67803936",
        [(1, 42.02, -87.67, "0"), (2, 42.01, -87.67, "328.084")],
    )
    poly, dist_m = load_gtfs_shape_with_dist(z, "67803936")
    assert poly.shape == (2, 2)
    assert dist_m is not None
    # 328.084 ft / 3.28084 == 100 m
    np.testing.assert_allclose(dist_m, [0.0, 100.0], atol=1e-6)


def test_load_gtfs_shape_with_dist_none_when_dist_absent(tmp_path):
    z = _gtfs_zip_with_shape(
        tmp_path / "gtfs.zip", "67803936",
        [(1, 42.02, -87.67, ""), (2, 42.01, -87.67, "")],
    )
    _poly, dist_m = load_gtfs_shape_with_dist(z, "67803936")
    assert dist_m is None


def test_shape_id_for_pattern_four_and_five_digit():
    assert shape_id_for_pattern("3936") == "67803936"   # 4-digit -> '6780' prefix
    assert shape_id_for_pattern("29251") == "67829251"  # 5-digit -> pad, not prefix
    assert shape_id_for_pattern(536) == "67800536"      # short pads to 5
