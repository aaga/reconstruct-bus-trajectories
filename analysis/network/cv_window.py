"""Pick a weekday headway-CV window + route set from the static GTFS.

A route qualifies for a window when BOTH directions are scheduled at
>= 4 departures in EVERY clock hour of the window (i.e. 15-min-or-better
average headway, hour by hour) at the route-direction's reference stop:
the stop served by the most of that day's trips (a busy timepoint, so
short-turns and branches are included where they overlap). Averaging
per hour, not max gap, keeps one 16-minute gap from disqualifying a
route that is otherwise every 8 minutes.

Candidate windows are whole hours inside [8, 18), >= 2 h long. For each
we count qualifying routes; the report lists the best window per length
so the trade-off (longer window -> fewer routes) is explicit.

Usage:
    PYTHONPATH=src uv run python analysis/network/cv_window.py --city nyc \
        --date 2026-09-16                 # canonical feed
    ... --era-dates 2026-05-13,2026-07-15 # also check other GTFS eras
"""
from __future__ import annotations

import argparse
import csv
import io
import sys
import zipfile
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from analysis.network import gtfs_history  # noqa: E402
from dataio.cities import get_city  # noqa: E402

LO_H, HI_H, MIN_LEN_H = 8, 18, 2
MIN_PER_HOUR = 4  # 15-min-or-better


def _rows(z, name):
    with z.open(name) as f:
        yield from csv.DictReader(io.TextIOWrapper(f, "utf-8-sig"))


def active_services(z, d: date) -> set[str]:
    wd = d.strftime("%A").lower()
    ds = d.strftime("%Y%m%d")
    # calendar.txt is optional in GTFS: RIPTA defines service in
    # calendar_dates.txt alone.
    on = ({r["service_id"] for r in _rows(z, "calendar.txt")
           if r[wd].strip() == "1" and r["start_date"].strip() <= ds <= r["end_date"].strip()}
          if "calendar.txt" in z.namelist() else set())
    if "calendar_dates.txt" in z.namelist():
        for r in _rows(z, "calendar_dates.txt"):
            if r["date"].strip() == ds:
                (on.add if r["exception_type"].strip() == "1" else on.discard)(r["service_id"])
    return on


def hourly_departures(gtfs_zip: Path, d: date, exclude_prefixes=()) -> dict:
    """route -> direction -> Counter(hour -> departures at reference stop)."""
    z = zipfile.ZipFile(gtfs_zip)
    svc = active_services(z, d)
    rtype = {r["route_id"]: r.get("route_type", "3") for r in _rows(z, "routes.txt")}
    trips = {}
    for r in _rows(z, "trips.txt"):
        rid = r["route_id"]
        if r["service_id"] in svc and rtype.get(rid) == "3" \
                and not any(rid.startswith(p) for p in exclude_prefixes):
            trips[r["trip_id"]] = (rid, r.get("direction_id", "0"))
    stop_trips = defaultdict(Counter)          # (route, dir) -> stop -> n trips
    times = defaultdict(list)                  # (route, dir, stop) -> minutes
    for r in _rows(z, "stop_times.txt"):
        t = trips.get(r["trip_id"])
        if t is None:
            continue
        hhmm = (r["departure_time"] or r["arrival_time"]).strip()
        if not hhmm:
            continue
        h, m, _ = hhmm.split(":")
        stop_trips[t][r["stop_id"]] += 1
        times[(*t, r["stop_id"])].append(int(h) * 60 + int(m))
    out = defaultdict(dict)
    for (rid, di), cnt in stop_trips.items():
        ref = cnt.most_common(1)[0][0]
        out[rid][di] = Counter(t // 60 for t in times[(rid, di, ref)])
    return out


def qualifying(hd: dict, a: int, b: int) -> list[str]:
    ok = []
    for rid, dirs in hd.items():
        if len(dirs) < 2:
            continue  # loops / one-way specials: no headway pair to judge
        if all(all(c[h] >= MIN_PER_HOUR for h in range(a, b)) for c in dirs.values()):
            ok.append(rid)
    return sorted(ok)


def scan(hd: dict) -> list[tuple[int, int, int]]:
    res = []
    for a in range(LO_H, HI_H - MIN_LEN_H + 1):
        for b in range(a + MIN_LEN_H, HI_H + 1):
            res.append((a, b, len(qualifying(hd, a, b))))
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--city", required=True)
    ap.add_argument("--date", required=True, help="representative weekday (canonical feed)")
    ap.add_argument("--era-dates", default="", help="comma list: also scan these dates' GTFS eras")
    a = ap.parse_args()
    city = get_city(a.city)
    runs = [(a.date, city.resolve(city.gtfs_zip))]
    for d in [x for x in a.era_dates.split(",") if x]:
        z = gtfs_history.era_zip(city, d)
        if z is not None:
            runs.append((d, z))
    for d, z in runs:
        hd = hourly_departures(z, date.fromisoformat(d), city.exclude_route_prefixes)
        n_routes = sum(1 for v in hd.values() if len(v) >= 2)
        res = scan(hd)
        print(f"\n{d} ({Path(z).name}): {n_routes} two-direction bus routes scheduled")
        best = {}
        for x, y, n in res:
            L = y - x
            if L not in best or n > best[L][2]:
                best[L] = (x, y, n)
        for L in sorted(best):
            x, y, n = best[L]
            print(f"  best {L:2d} h window: {x:02d}:00-{y:02d}:00  {n:3d} routes ({100*n/n_routes:.0f}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
