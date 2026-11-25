"""Merge several GTFS feeds of one network into a single feed.

MTA publishes NYC buses as six feeds (five NYCT boroughs + MTA Bus Co.).
Every NYCT feed repeats the same agency/routes tables, and borough feeds
share some stops, while trips, calendars and shapes are disjoint (verified
2026-09-29 on the Fall 2026 feeds: 307 shared routes, 940 shared stops,
0 shared trip/service/shape ids). So the merge is: union columns, keep the
first row per primary key in the shared tables, concatenate the rest.

Duplicate keys whose rows DIFFER (beyond column order) are counted and
reported rather than silently dropped, so a real id collision is visible.
"""
from __future__ import annotations

import csv
import io
import zipfile
from pathlib import Path

# file -> primary-key columns; files not listed are concatenated as-is
KEYS: dict[str, tuple[str, ...]] = {
    "agency.txt": ("agency_id",),
    "routes.txt": ("route_id",),
    "stops.txt": ("stop_id",),
    "calendar.txt": ("service_id",),
    "calendar_dates.txt": ("service_id", "date"),
    "shapes.txt": ("shape_id", "shape_pt_sequence"),
    "trips.txt": ("trip_id",),
    "stop_times.txt": ("trip_id", "stop_sequence"),
    "feed_info.txt": ("feed_publisher_name", "feed_version"),
}


def merge_feeds(inputs: list[Path], out: Path,
                files: tuple[str, ...] | None = None) -> dict:
    """Write the merged feed to ``out``; returns per-file stats.

    ``files`` limits which members are merged (default: every .txt member
    present in any input).
    """
    zips = [zipfile.ZipFile(p) for p in inputs]
    members = sorted({n for z in zips for n in z.namelist() if n.endswith(".txt")})
    if files is not None:
        members = [m for m in members if m in files]
    stats: dict[str, dict] = {}
    tmp = out.with_suffix(".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
        for m in members:
            cols: list[str] = []
            rows: list[dict] = []
            for z in zips:
                if m not in z.namelist():
                    continue
                with z.open(m) as f:
                    rd = csv.DictReader(io.TextIOWrapper(f, "utf-8-sig"))
                    for c in rd.fieldnames or []:
                        if c not in cols:
                            cols.append(c)
                    rows.extend(rd)
            key = KEYS.get(m)
            n_in, n_dup, n_conflict = len(rows), 0, 0
            if key and all(k in cols for k in key):
                seen: dict[tuple, dict] = {}
                kept = []
                for r in rows:
                    k = tuple(r.get(c, "") for c in key)
                    if k in seen:
                        n_dup += 1
                        a = {c: (seen[k].get(c) or "") for c in cols}
                        b = {c: (r.get(c) or "") for c in cols}
                        if a != b:
                            n_conflict += 1
                        continue
                    seen[k] = r
                    kept.append(r)
                rows = kept
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore",
                               lineterminator="\n")
            w.writeheader()
            for r in rows:
                w.writerow({c: r.get(c, "") for c in cols})
            zout.writestr(m, buf.getvalue())
            stats[m] = {"rows_in": n_in, "rows_out": len(rows),
                        "dup_keys": n_dup, "conflicting_dups": n_conflict}
    tmp.replace(out)
    return stats


def main() -> int:
    import argparse
    import json

    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("out")
    ap.add_argument("inputs", nargs="+")
    a = ap.parse_args()
    st = merge_feeds([Path(p) for p in a.inputs], Path(a.out))
    print(json.dumps(st, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
