"""Shard route-id dtype widens past 255 routes (build_payloads.shard_dtypes)."""
import json
import struct
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from analysis.network import build_payloads as bp  # noqa: E402


def _read(path):
    """Decode a shard exactly as the dashboard does: dtypes from the header."""
    buf = path.read_bytes()
    hlen = struct.unpack("<I", buf[8:12])[0]
    head = json.loads(buf[12:12 + hlen])
    off, cols = 12 + hlen, {}
    for c in head["columns"]:
        dt = np.dtype(c["dtype"])
        cols[c["name"]] = np.frombuffer(buf, dt, head["n_rows"], off)
        off += dt.itemsize * head["n_rows"]
    return head, cols


def _rows(rid):
    n = len(rid)
    rows = {k: np.zeros(n) for k in bp.SHARD_DTYPES}
    rows["rid"] = np.asarray(rid)
    rows["sid"] = np.arange(n)
    return rows


def test_small_city_keeps_u1(tmp_path):
    d = bp.shard_dtypes(150)
    assert d["rid"] == "<u1"
    bp._write_shard(tmp_path / "s.bin", _rows([0, 5, 149]), d)
    head, cols = _read(tmp_path / "s.bin")
    assert [c["dtype"] for c in head["columns"] if c["name"] == "rid"] == ["<u1"]
    assert cols["rid"].tolist() == [0, 5, 149]


def test_large_city_widens_rid_without_clamping(tmp_path):
    d = bp.shard_dtypes(400)
    assert d["rid"] == "<u2"
    bp._write_shard(tmp_path / "s.bin", _rows([0, 255, 256, 399]), d)
    _, cols = _read(tmp_path / "s.bin")
    assert cols["rid"].tolist() == [0, 255, 256, 399]
    assert bp._row_bytes(d) == bp._row_bytes(bp.SHARD_DTYPES) + 1
