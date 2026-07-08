"""Tests for shape-id enumeration helpers in io.py.

Uses the bundled CTA GTFS zip when present (skip otherwise so CI without
data still passes).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dataio.gtfs import list_bus_shapes, list_shape_ids


REPO_ROOT = Path(__file__).resolve().parent.parent
GTFS_ZIP = REPO_ROOT / "cta_gtfs.zip"
pytestmark = pytest.mark.skipif(
    not GTFS_ZIP.exists(),
    reason="CTA GTFS zip not present; integration data missing",
)


def test_list_bus_shapes_includes_known_pattern():
    bus_shapes = set(list_bus_shapes(GTFS_ZIP))
    # Pattern 3936 = Route 22 SB (Clark southbound) — covered throughout the
    # rest of the test suite.
    assert "67803936" in bus_shapes
    assert "67803932" in bus_shapes  # Route 22 NB
    # CTA bus catalogue is dozens to hundreds of distinct shapes.
    assert len(bus_shapes) >= 50


def test_list_shape_ids_filter_distinguishes_route_types():
    bus_only = set(list_shape_ids(GTFS_ZIP, route_type="3"))
    all_shapes = set(list_shape_ids(GTFS_ZIP, route_type=None))
    # Bus shapes are a subset of all shapes.
    assert bus_only.issubset(all_shapes)
