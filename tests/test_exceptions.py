"""exceptions.json stays loadable and schema-valid."""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from dataio.exceptions import (  # noqa: E402
    EXCEPTIONS_PATH,
    ExceptionsError,
    _validate_entry,
    load_exceptions,
)


def test_committed_file_valid():
    ex = load_exceptions("cta")
    assert ex.city == "cta"


def test_unknown_type_rejected():
    bad = {"id": "x", "type": "nope", "city": "cta", "target": {"stop_id": "1"},
           "why": "w", "added": "2026-08-10"}
    with pytest.raises(ExceptionsError):
        _validate_entry(bad, set())


def test_coord_override_needs_lat_lon():
    bad = {"id": "x", "type": "stop_coord_override", "city": "cta",
           "target": {"stop_id": "1"}, "value": {"lat": 41.0},
           "why": "w", "added": "2026-08-10"}
    with pytest.raises(ExceptionsError):
        _validate_entry(bad, set())


def test_example_entry_roundtrip(tmp_path):
    doc = {"version": 1, "entries": [{
        "id": "cta-6515-bad-door-peak",
        "type": "door_peak_reject",
        "city": "cta",
        "target": {"stop_id": "6515"},
        "why": "peak lands a block away at the Red Line station",
        "added": "2026-08-10",
    }]}
    p = tmp_path / "exceptions.json"
    p.write_text(json.dumps(doc))
    ex = load_exceptions("cta", path=p)
    assert "6515" in ex.peak_rejects
    assert load_exceptions("mbta", path=p).peak_rejects == frozenset()


def test_file_exists():
    assert EXCEPTIONS_PATH.exists()
