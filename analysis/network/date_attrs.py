"""Per-service-date attributes: day-of-week, daytype, season, pick, weather.

One row per service date in the archive window. Weather comes from NOAA's
NCEI daily-summaries API for the city's GHCN-D station (cached under
``caches/weather/``); holidays count as their own daytype so peak metrics
aren't diluted by holiday service running on a weekday date.

Also provides ``print_pick_report`` — scheduled-trips-per-service-date from
GTFS calendar.txt — to confirm the configured pick boundaries against the
feed's own service ramps.

Usage:
    PYTHONPATH=src uv run python analysis/network/date_attrs.py --city cta \
        --start 2026-04-27 --end 2026-07-21
    PYTHONPATH=src uv run python analysis/network/date_attrs.py --city cta --pick-report
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

# US federal holidays in the archive window (extend as the archive grows).
HOLIDAYS_2026 = {
    "2026-01-01", "2026-01-19", "2026-02-16", "2026-05-25",
    "2026-07-03", "2026-07-04", "2026-09-07", "2026-11-26",
    "2026-12-25",
}


def season_of(d: date) -> str:
    """Meteorological season."""
    return {12: "winter", 1: "winter", 2: "winter",
            3: "spring", 4: "spring", 5: "spring",
            6: "summer", 7: "summer", 8: "summer",
            9: "fall", 10: "fall", 11: "fall"}[d.month]


def daytype_of(d: date) -> str:
    if d.isoformat() in HOLIDAYS_2026:
        return "holiday"
    return {5: "sat", 6: "sun"}.get(d.weekday(), "weekday")


def load_weather(city: CityConfig, start: str, end: str) -> dict[str, str]:
    """date_iso -> {dry|rain|snow} from GHCN-D daily summaries (cached)."""
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
            "daytype": daytype_of(d),
            "season": season_of(d),
            "pick": city.pick_for_date(iso),
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


def print_pick_report(city: CityConfig) -> None:
    """Scheduled trips per service date from calendar.txt (+ calendar_dates),
    so the user can see the feed's service ramps and confirm pick boundaries."""
    gtfs_zip = city.resolve(city.gtfs_zip)
    with zipfile.ZipFile(gtfs_zip) as z:
        with z.open("trips.txt") as f:
            trips_per_service: Counter = Counter()
            for t in csv.DictReader(_io.TextIOWrapper(f, encoding="utf-8-sig")):
                trips_per_service[t["service_id"]] += 1
        with z.open("calendar.txt") as f:
            cal = list(csv.DictReader(_io.TextIOWrapper(f, encoding="utf-8-sig")))

    print(f"pick config: {[(p.pick_id, p.start_date) for p in city.picks]}\n")
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
    ap.add_argument("--pick-report", action="store_true")
    args = ap.parse_args()

    city = get_city(args.city)
    if args.pick_report:
        print_pick_report(city)
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
