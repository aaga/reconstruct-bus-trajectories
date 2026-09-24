"""Historical GTFS feeds from Transitland, one era per service date.

CTA republishes its feed every few weeks and the geometry genuinely moves:
between the 2023-12 and 2026-06 feeds, 58% of shared routes changed shape
count and 25% moved median shape length by >10% (route 53's median shape
length nearly doubled as short-turn variants were dropped). Matching 2024
pings against the 2026 snapshot would silently mis-assign those trips, and
would drop routes that no longer exist (route 5) while missing seasonal
ones the current snapshot lacks (10, 130).

Every feed version is stored as a plain GTFS zip, so all of ``dataio.gtfs``
(``list_bus_shapes``, ``load_gtfs_shape_with_dist``, ``load_route_stops``)
works against an era by passing its path — no parallel loader.

Feed choice for a date: of the versions whose calendar range covers it,
take the one most recently FETCHED on or before that date, i.e. what CTA
was actually publishing that day. Coverage of 2024-01-01..2026-08-06 was
verified complete — 949/949 days, 40 versions, zero gaps.

The API key is read from $TRANSITLAND_API_KEY or caches/transitland.env
(both gitignored); it is never written into the repo.

Usage:
    PYTHONPATH=src uv run python analysis/network/gtfs_history.py \
        --city cta --start 2024-01-01 --end 2026-08-06
    ... --index-only     # rebuild date->era index from what's cached
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from dataio.cities import get_city  # noqa: E402

API = "https://transit.land/api/v2/rest"
FEED_OSID = {"cta": "f-dp3-cta"}
# Files the pipeline actually reads; the rest (license html, transfers,
# frequencies) are dropped to keep the cache near 3 GB instead of 4 GB.
KEEP = ("shapes.txt", "trips.txt", "routes.txt", "stops.txt",
        "stop_times.txt", "calendar.txt", "calendar_dates.txt")


def _api_key() -> str:
    key = os.environ.get("TRANSITLAND_API_KEY", "").strip()
    if key:
        return key
    env = REPO / "caches" / "transitland.env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("TRANSITLAND_API_KEY="):
                return line.split("=", 1)[1].strip()
    raise SystemExit(
        "no Transitland API key: set $TRANSITLAND_API_KEY or write "
        "TRANSITLAND_API_KEY=... to caches/transitland.env")


def list_versions(feed_osid: str, key: str) -> list[dict]:
    """Every feed version, oldest fetch first."""
    url = f"{API}/feed_versions?feed_onestop_id={feed_osid}&limit=100&apikey={key}"
    out: list[dict] = []
    while url:
        with urllib.request.urlopen(url) as r:
            d = json.load(r)
        out += d.get("feed_versions", [])
        url = (d.get("meta") or {}).get("next")
    out.sort(key=lambda v: v["fetched_at"])
    return out


def pick_for_date(versions: list[dict], date_iso: str) -> dict | None:
    """Version live on date_iso: latest fetched on/before it that covers it."""
    best = None
    for v in versions:
        e, l = v.get("earliest_calendar_date"), v.get("latest_calendar_date")
        if not (e and l and e <= date_iso <= l):
            continue
        if v["fetched_at"][:10] <= date_iso:
            if best is None or v["fetched_at"] > best["fetched_at"]:
                best = v
    if best is not None:
        return best
    # Date precedes every fetch that covers it (early in the window): fall
    # back to the earliest covering version rather than leaving a hole.
    cover = [v for v in versions
             if v.get("earliest_calendar_date") and v.get("latest_calendar_date")
             and v["earliest_calendar_date"] <= date_iso <= v["latest_calendar_date"]]
    return min(cover, key=lambda v: v["fetched_at"]) if cover else None


def _download(sha1: str, dest: Path, key: str) -> bool:
    """Fetch a feed version and keep only KEEP members. True if written."""
    tmp = dest.with_suffix(".download")
    url = f"{API}/feed_versions/{sha1}/download?apikey={key}"
    req = urllib.request.Request(url, headers={"User-Agent": "jtl-bus/1.0"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=600) as r, open(tmp, "wb") as f:
                while chunk := r.read(1 << 20):
                    f.write(chunk)
            break
        except Exception as e:  # noqa: BLE001
            print(f"    attempt {attempt + 1} failed: {e}", flush=True)
            tmp.unlink(missing_ok=True)
            time.sleep(5 * (attempt + 1))
    else:
        return False
    try:
        with zipfile.ZipFile(tmp) as zin:
            names = set(zin.namelist())
            with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zout:
                for m in KEEP:
                    if m in names:
                        zout.writestr(m, zin.read(m))
    except Exception as e:  # noqa: BLE001
        print(f"    bad zip: {e}", flush=True)
        dest.unlink(missing_ok=True)
        return False
    finally:
        tmp.unlink(missing_ok=True)
    return True


def sync(city_id: str, start: str, end: str, index_only: bool = False) -> None:
    city = get_city(city_id)
    osid = FEED_OSID.get(city_id)
    if not osid:
        raise SystemExit(f"no Transitland feed onestop id known for {city_id}")
    if not city.gtfs_history_dir:
        raise SystemExit(f"{city_id} has no gtfs_history_dir configured")
    out = city.resolve(city.gtfs_history_dir)
    out.mkdir(parents=True, exist_ok=True)
    key = _api_key()

    vers = list_versions(osid, key)
    print(f"{len(vers)} feed versions on Transitland for {osid}")

    # Which versions are needed to cover [start, end]?
    import datetime as dt
    d0, d1 = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    need: dict[str, dict] = {}
    index: dict[str, str] = {}
    day = d0
    missing = 0
    while day <= d1:
        iso = day.isoformat()
        v = pick_for_date(vers, iso)
        if v is None:
            missing += 1
        else:
            sha8 = v["sha1"][:8]
            need[sha8] = v
            index[iso] = sha8
        day += dt.timedelta(days=1)
    print(f"{len(index):,} dates mapped to {len(need)} feed versions"
          + (f"; {missing} dates UNCOVERED" if missing else "; no gaps"))

    if not index_only:
        for i, (sha8, v) in enumerate(sorted(need.items()), 1):
            dest = out / f"{sha8}.zip"
            if dest.exists() and dest.stat().st_size > 0:
                continue
            print(f"  [{i}/{len(need)}] {sha8} "
                  f"({v['earliest_calendar_date']} -> {v['latest_calendar_date']})",
                  flush=True)
            if not _download(v["sha1"], dest, key):
                print(f"    GIVING UP on {sha8}", flush=True)

    have = {p.stem for p in out.glob("*.zip") if p.stat().st_size > 0}
    index = {d: s for d, s in index.items() if s in have}
    meta = {
        "feed_onestop_id": osid,
        "generated_for": {"start": start, "end": end},
        "eras": {
            s: {
                "sha1": v["sha1"],
                "fetched_at": v["fetched_at"][:10],
                "earliest_calendar_date": v.get("earliest_calendar_date"),
                "latest_calendar_date": v.get("latest_calendar_date"),
            }
            for s, v in need.items() if s in have
        },
        "dates": index,
    }
    (out / "index.json").write_text(json.dumps(meta, indent=1, sort_keys=True))
    tot = sum(p.stat().st_size for p in out.glob("*.zip"))
    print(f"cached {len(have)} eras, {tot / 2**30:.1f} GB -> {out}")
    print(f"index covers {len(index):,} dates")


# ---------------------------------------------------------------------------
# Read side (used by the batches)
# ---------------------------------------------------------------------------

_INDEX: dict[str, dict] = {}


def era_zip(city, date_iso: str) -> Path | None:
    """GTFS zip for the era covering ``date_iso``; None if not cached.

    Falls back to the city's single ``gtfs_zip`` when no history is set up,
    so existing single-snapshot cities and tests behave unchanged.
    """
    if not city.gtfs_history_dir:
        return city.resolve(city.gtfs_zip)
    base = city.resolve(city.gtfs_history_dir)
    idx = _INDEX.get(str(base))
    if idx is None:
        f = base / "index.json"
        if not f.exists():
            return city.resolve(city.gtfs_zip)
        idx = json.loads(f.read_text())
        _INDEX[str(base)] = idx
    sha8 = idx.get("dates", {}).get(date_iso)
    if sha8 is None:
        return None
    p = base / f"{sha8}.zip"
    return p if p.exists() else None


def era_id(city, date_iso: str) -> str | None:
    """Short era key (feed sha8) for a service date."""
    if not city.gtfs_history_dir:
        return None
    base = city.resolve(city.gtfs_history_dir)
    idx = _INDEX.get(str(base))
    if idx is None:
        f = base / "index.json"
        if not f.exists():
            return None
        idx = json.loads(f.read_text())
        _INDEX[str(base)] = idx
    return idx.get("dates", {}).get(date_iso)


def all_eras(city) -> dict[str, dict]:
    """era sha8 -> metadata, for batches that iterate eras."""
    if not city.gtfs_history_dir:
        return {}
    f = city.resolve(city.gtfs_history_dir) / "index.json"
    if not f.exists():
        return {}
    return json.loads(f.read_text()).get("eras", {})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--city", default="cta")
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--end", default="2026-08-06")
    ap.add_argument("--index-only", action="store_true")
    a = ap.parse_args()
    sync(a.city, a.start, a.end, a.index_only)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
