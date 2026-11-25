"""Decide per city whether traversals apply the pre-departure head trim.

The trim (``assign_shapes.pre_departure_cut``) drops the leading pings that
still report a trip's first stop sequence before choosing its shape. It is
a clear win where the feed attaches the trip id during the deadhead and
layover (MBTA), and a loss where the sequence advances late and the dropped
head is what tells route variants apart (TransLink). So it is measured, not
assumed: on a sample of service dates, every trip is assigned twice — all
pings vs trimmed — and both choices are scored against the SCHEDULED shape
(GTFS-rt trip_id -> trips.txt of that date's era). Two shapes agree when
their segment sequences are identical (duplicate geometries are common).

Verdict -> outputs/network/<city>/head_trim.json, read by run_reconstruct's
workers (missing file = trim off). Not applicable (trim off) when the feed
carries no stop sequence or its trip ids don't join the schedule (CTA AVL).

Usage:
    PYTHONPATH=src uv run python analysis/network/head_trim_check.py --city mbta
    ... --dates 2026-05-20,2026-07-15 --trips-per-date 1500
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import time
import zipfile
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from analysis.network import run_reconstruct as rr  # noqa: E402
from analysis.network.assign_shapes import (  # noqa: E402
    HEAD_TRIM_MIN_FIX_RATIO,
    HEAD_TRIM_MIN_NET_GAIN,
    choose_shape,
    choose_shape_head_trimmed,
    decide_head_trim,
    pre_departure_cut,
)
from dataio.cities import get_city  # noqa: E402

MIN_JOIN_RATE = 0.5  # below this the schedule can't serve as ground truth
N_DEFAULT_DATES = 3


def verdict_path(city) -> Path:
    return REPO / "outputs" / "network" / city.city_id / "head_trim.json"


def load_verdict(city) -> bool:
    """True if this city's traversals should apply the trim."""
    p = verdict_path(city)
    if not p.exists():
        return False
    try:
        return bool(json.loads(p.read_text()).get("apply", False))
    except Exception:  # noqa: BLE001
        return False


def _default_dates(city) -> list[str]:
    """Weekdays spread across the archive (Wednesdays nearest to 25/50/75%)."""
    import pandas as pd

    dates = rr._dates_in_archive(city)
    wed = [d for d in dates if pd.Timestamp(d).dayofweek == 2]
    pool = wed or dates
    if not pool:
        return []
    qs = np.linspace(0, len(pool) - 1, N_DEFAULT_DATES + 2)[1:-1]
    return sorted({pool[int(round(q))] for q in qs})


def _scheduled_shapes(gtfs_zip: Path) -> dict[str, str]:
    with zipfile.ZipFile(gtfs_zip) as z, z.open("trips.txt") as f:
        return {r["trip_id"]: r["shape_id"]
                for r in csv.DictReader(io.TextIOWrapper(f, "utf-8-sig"))}


def check_date(date_iso: str, n_trips: int, seed: int) -> dict:
    rr._set_era(date_iso)
    shapes = rr._G["shapes"]
    sched = _scheduled_shapes(rr._G["gtfs_zip"])
    segseq = {s: tuple(r["seg_seq"]) for s, r in shapes.items()}
    df = rr._service_date_pings(rr._G["city"], date_iso)
    out = {"date": date_iso, "n_trips_seen": 0, "n_joined": 0, "n_scored": 0,
           "full_ok": 0, "trim_ok": 0, "fixed": 0, "broken": 0,
           "n_trimmed": 0, "has_seq": "current_stop_sequence" in df.columns}
    if df.empty or not out["has_seq"]:
        return out
    groups = list(df.sort_values("ts_utc").groupby(["trip_id", "vehicle_id"]))
    out["n_trips_seen"] = len(groups)
    rng = np.random.default_rng(seed)
    for i in rng.choice(len(groups), size=min(n_trips, len(groups)), replace=False):
        (tid, _veh), t = groups[i]
        truth = sched.get(str(tid))
        if truth is None:
            continue
        out["n_joined"] += 1
        t = t.drop_duplicates(subset="ts_utc")
        cands = rr._G["shapes_by_route"].get(str(t.route_id.iloc[0]), [])
        if truth not in segseq or not cands or len(t) < rr.MIN_PINGS:
            continue
        mm = {s: rr._matcher(s)[0] for s in cands}
        ll = {s: rr._matcher(s)[1] for s in cands}
        lat = t.latitude.to_numpy(float)
        lon = t.longitude.to_numpy(float)
        seq = t.current_stop_sequence.to_numpy()
        a = choose_shape(lat, lon, mm, ll)
        b = choose_shape_head_trimmed(lat, lon, seq, mm, ll)
        ok_a = not isinstance(a, str) and segseq.get(a.shape_id) == segseq[truth]
        ok_b = not isinstance(b, str) and segseq.get(b.shape_id) == segseq[truth]
        out["n_scored"] += 1
        out["n_trimmed"] += int(pre_departure_cut(seq) > 0)
        out["full_ok"] += ok_a
        out["trim_ok"] += ok_b
        out["fixed"] += (not ok_a) and ok_b
        out["broken"] += ok_a and (not ok_b)
    return out


def run(city_id: str, dates: list[str] | None, n_trips: int) -> dict:
    city = get_city(city_id)
    rr._init_worker(city_id)
    dates = dates or _default_dates(city)
    per_date = []
    for i, d in enumerate(dates):
        t0 = time.time()
        r = check_date(d, n_trips, seed=i)
        per_date.append(r)
        print(f"  {d}: scored {r['n_scored']:,}  full ok {r['full_ok']:,}  "
              f"trim ok {r['trim_ok']:,}  fixed {r['fixed']}  broken {r['broken']}"
              f"  ({time.time() - t0:.0f}s)", flush=True)
    tot = {k: sum(r[k] for r in per_date)
           for k in ("n_trips_seen", "n_joined", "n_scored", "full_ok",
                     "trim_ok", "fixed", "broken", "n_trimmed")}
    sampled = min(n_trips * len(dates), max(tot["n_trips_seen"], 1))
    join_rate = tot["n_joined"] / max(sampled, 1)
    if not any(r["has_seq"] for r in per_date):
        apply, reason = False, "feed carries no current_stop_sequence"
    elif join_rate < MIN_JOIN_RATE:
        apply, reason = False, (f"trip ids join the schedule for only "
                                f"{join_rate:.0%} of trips — no ground truth")
    else:
        apply = decide_head_trim(tot["n_scored"], tot["fixed"], tot["broken"])
        reason = ("trim clearly improves agreement with the schedule" if apply
                  else "trim does not clearly improve agreement with the schedule")
    n = max(tot["n_scored"], 1)
    verdict = {
        "city": city.city_id,
        "apply": apply,
        "reason": reason,
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "criteria": {"min_net_gain": HEAD_TRIM_MIN_NET_GAIN,
                     "min_fix_ratio": HEAD_TRIM_MIN_FIX_RATIO},
        "schedule_join_rate": round(join_rate, 4),
        "full_match_rate": round(tot["full_ok"] / n, 4),
        "trim_match_rate": round(tot["trim_ok"] / n, 4),
        "net_gain": round((tot["fixed"] - tot["broken"]) / n, 4),
        **tot,
        "per_date": per_date,
    }
    p = verdict_path(city)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(verdict, indent=1))
    print(f"head_trim[{city.city_id}]: apply={apply} — {reason}\n"
          f"  schedule match {verdict['full_match_rate']:.2%} -> "
          f"{verdict['trim_match_rate']:.2%} (fixed {tot['fixed']}, broken "
          f"{tot['broken']}, n={tot['n_scored']:,}) -> {p}")
    return verdict


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--city", required=True)
    ap.add_argument("--dates", default=None, help="comma list (default: 3 spread Wednesdays)")
    ap.add_argument("--trips-per-date", type=int, default=1500)
    a = ap.parse_args()
    run(a.city, a.dates.split(",") if a.dates else None, a.trips_per_date)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
