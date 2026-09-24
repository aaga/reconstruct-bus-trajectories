"""Per-service-date attributes: day-of-week, daytype, season, weather.

One row per service date in the archive window. Weather comes from NOAA's
NCEI daily-summaries API for the city's GHCN-D station (cached under
``caches/weather/``); holidays count as their own daytype so peak metrics
aren't diluted by holiday service running on a weekday date.

Also provides ``print_service_report`` — scheduled-trips-per-service-date
from GTFS calendar.txt — to inspect the feed's service ramps (useful when
choosing date filters or a feed vintage).

Usage:
    PYTHONPATH=src uv run python analysis/network/date_attrs.py --city cta \
        --start 2026-04-27 --end 2026-07-21
    PYTHONPATH=src uv run python analysis/network/date_attrs.py --city cta --service-report
Output:
    outputs/network/<city>/date_attrs.json
"""

from __future__ import annotations

import argparse
import csv
import io as _io
import json
import sys
import zipfile
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from dataio.cities import CityConfig, get_city  # noqa: E402
from dataio.realtime import fetch  # noqa: E402

# Precip thresholds (mm) for daily weather buckets.
SNOW_MM = 2.5
RAIN_MM = 2.5

NCEI_URL = (
    "https://www.ncei.noaa.gov/access/services/data/v1"
    "?dataset=daily-summaries&stations={station}&dataTypes=PRCP,SNOW,TMAX"
    "&startDate={start}&endDate={end}&format=csv&units=metric"
)

# Holidays in the archive window by region (extend as the archive grows).
# US: federal, 2024-01 → 2026 for the historical CTA pass; CA-BC: British
# Columbia statutory (TransLink). Holiday service runs a Sunday-like
# schedule on a weekday date, so these get their own daytype.
HOLIDAYS_2026 = {
    "US": {
        # 2024
        "2024-01-01", "2024-01-15", "2024-02-19", "2024-05-27",
        "2024-06-19", "2024-07-04", "2024-09-02", "2024-11-28",
        "2024-11-29", "2024-12-24", "2024-12-25",
        # 2025
        "2025-01-01", "2025-01-20", "2025-02-17", "2025-05-26",
        "2025-06-19", "2025-07-04", "2025-09-01", "2025-11-27",
        "2025-11-28", "2025-12-24", "2025-12-25",
        # 2026
        "2026-01-01", "2026-01-19", "2026-02-16", "2026-05-25",
        "2026-06-19", "2026-07-03", "2026-07-04", "2026-09-07",
        "2026-11-26", "2026-11-27", "2026-12-24", "2026-12-25",
    },
    "CA-BC": {
        "2026-01-01", "2026-02-16", "2026-04-03", "2026-05-18",
        "2026-07-01", "2026-08-03", "2026-09-07", "2026-09-30",
        "2026-10-12", "2026-11-11", "2026-12-25",
    },
}


def _era_pick(city: CityConfig, iso: str) -> str | None:
    """GTFS era (feed sha8) live on a date, as a pick label."""
    try:
        from analysis.network import gtfs_history

        e = gtfs_history.era_id(city, iso)
        return f"era_{e}" if e else None
    except Exception:  # noqa: BLE001 — history is optional
        return None


def season_of(d: date) -> str:
    """Meteorological season."""
    return {12: "winter", 1: "winter", 2: "winter",
            3: "spring", 4: "spring", 5: "spring",
            6: "summer", 7: "summer", 8: "summer",
            9: "fall", 10: "fall", 11: "fall"}[d.month]


def daytype_of(d: date, region: str = "US") -> str:
    if d.isoformat() in HOLIDAYS_2026[region]:
        return "holiday"
    return {5: "sat", 6: "sun"}.get(d.weekday(), "weekday")


def load_weather(city: CityConfig, start: str, end: str) -> dict[str, str]:
    """date_iso -> {dry|rain|snow} from GHCN-D daily summaries (cached)."""
    if not city.noaa_station:
        # No usable station (Vancouver GHCN-D has no 2026 precipitation);
        # every day buckets "unknown" and the weather filter stays inert.
        print("no noaa_station configured; all days bucketed 'unknown'")
        return {}
    cache = REPO / "caches" / "weather" / f"{city.noaa_station}_{start}_{end}.csv"
    fetch(NCEI_URL.format(station=city.noaa_station, start=start, end=end), cache)
    out: dict[str, str] = {}
    try:
        df = pd.read_csv(cache)
    except Exception as e:  # noqa: BLE001
        print(f"weather load failed ({e}); all days bucketed 'unknown'", file=sys.stderr)
        return out
    for _, r in df.iterrows():
        snow = float(r.get("SNOW") or 0.0)
        prcp = float(r.get("PRCP") or 0.0)
        bucket = "snow" if snow >= SNOW_MM else ("rain" if prcp >= RAIN_MM else "dry")
        out[str(r["DATE"])] = bucket
    return out


def build_date_attrs(city: CityConfig, start: str, end: str) -> dict:
    weather = load_weather(city, start, end)
    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    days = {}
    d = d0
    while d <= d1:
        iso = d.isoformat()
        days[iso] = {
            "dow": d.weekday(),  # 0=Mon .. 6=Sun
            "daytype": daytype_of(d, city.holiday_region),
            "season": season_of(d),
            # Pick config is gone (2026-08-26); the GTFS era (feed version)
            # live that day is the real service-change boundary anyway, and
            # build_facts still reads it under the "pick" key.
            "pick": _era_pick(city, iso),
            "weather": weather.get(iso, "unknown"),
        }
        d += timedelta(days=1)
    return {
        "meta": {
            "city": city.city_id,
            "start": start,
            "end": end,
            "noaa_station": city.noaa_station,
            "n_days": len(days),
            "weather_counts": dict(Counter(v["weather"] for v in days.values())),
        },
        "days": days,
    }


def print_service_report(city: CityConfig) -> None:
    """Scheduled trips per service date from calendar.txt (+ calendar_dates),
    so the user can see the feed's service ramps."""
    gtfs_zip = city.resolve(city.gtfs_zip)
    with zipfile.ZipFile(gtfs_zip) as z:
        with z.open("trips.txt") as f:
            trips_per_service: Counter = Counter()
            for t in csv.DictReader(_io.TextIOWrapper(f, encoding="utf-8-sig")):
                trips_per_service[t["service_id"]] += 1
        with z.open("calendar.txt") as f:
            cal = list(csv.DictReader(_io.TextIOWrapper(f, encoding="utf-8-sig")))

    print("service_id ranges (calendar.txt) with scheduled trip counts:")
    for r in sorted(cal, key=lambda r: (r["start_date"], r["service_id"])):
        dows = "".join(
            d[0].upper() if r[d] == "1" else "-"
            for d in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
        )
        n = trips_per_service.get(r["service_id"], 0)
        if n:
            print(f"  {r['service_id']:>8}  {r['start_date']} .. {r['end_date']}  {dows}  {n:6d} trips")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--city", default="cta")
    ap.add_argument("--start", default="2026-04-27")
    ap.add_argument("--end", default=None, help="default: today")
    ap.add_argument("--service-report", action="store_true")
    args = ap.parse_args()

    city = get_city(args.city)
    if args.service_report:
        print_service_report(city)
        return

    end = args.end or date.today().isoformat()
    payload = build_date_attrs(city, args.start, end)
    out = REPO / "outputs" / "network" / city.city_id / "date_attrs.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=1))
    print(f"wrote {out}")
    print(json.dumps(payload["meta"], indent=1))


if __name__ == "__main__":
    main()
