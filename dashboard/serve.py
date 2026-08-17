"""Static server for the dashboard, with HTTP Range support.

``python3 -m http.server`` ignores Range headers and answers 200 with the
whole body, so DuckDB-WASM would pull every monthly parquet in full on
every query instead of the few row groups a slice touches. This adds 206
partial-content handling (and no-cache, so a rebuilt payload is picked up
on reload rather than served stale from the heuristic freshness window).

    uv run python dashboard/serve.py [--port 8931]
"""
from __future__ import annotations

import argparse
import os
import re
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)")


class RangeHandler(SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def send_head(self):
        rng = self.headers.get("Range")
        if not rng:
            return super().send_head()
        path = self.translate_path(self.path)
        if os.path.isdir(path):
            return super().send_head()
        try:
            f = open(path, "rb")
        except OSError:
            self.send_error(404, "File not found")
            return None
        size = os.fstat(f.fileno()).st_size
        m = RANGE_RE.fullmatch(rng.strip())
        if not m:
            f.close()
            self.send_error(400, "Malformed Range")
            return None
        start_s, end_s = m.group(1), m.group(2)
        if start_s:
            start = int(start_s)
            end = int(end_s) if end_s else size - 1
        else:  # suffix range: last N bytes
            n = int(end_s or 0)
            start, end = max(0, size - n), size - 1
        if start >= size:
            f.close()
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return None
        end = min(end, size - 1)
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        f.seek(start)
        return _Slice(f, end - start + 1)

    def log_message(self, fmt, *args):  # keep the console readable
        if "?" not in self.path or self.command != "GET":
            super().log_message(fmt, *args)


class _Slice:
    """File-like wrapper copyfile() can stream a bounded slice from."""

    def __init__(self, f, remaining: int):
        self.f, self.remaining = f, remaining

    def read(self, n=-1):
        if self.remaining <= 0:
            return b""
        if n < 0 or n > self.remaining:
            n = self.remaining
        data = self.f.read(n)
        self.remaining -= len(data)
        return data

    def close(self):
        self.f.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8931)
    ap.add_argument("--dir", default=str(Path(__file__).resolve().parent))
    a = ap.parse_args()
    handler = partial(RangeHandler, directory=a.dir)
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), handler)
    print(f"serving {a.dir} with Range support on http://localhost:{a.port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
