"""Profiled end-to-end network pipeline driver (new-city capable).

Runs every stage in dependency order, each as a subprocess, and records a
per-stage profile (wall clock, child user/sys CPU, cumulative peak RSS,
exit status, key output counts) to::

    outputs/network/<city>/pipeline_profile.json      (rewritten per stage)
    outputs/network/<city>/pipeline_logs/<stage>.log  (full stage output)

Stages (door stages auto-skip when ``city.has_door_data`` is False):

    prefetch        R2 hour-files → local cache
    way_match       Valhalla map-snap per GTFS shape   (stage 1)
    intersections   PBF walk → control points          (stage 2)
    way_geoms       PBF → OSM way geometries
    registry        canonical signal-to-signal segments
    traversals      86-day reconstruction batch
    door_join       APC sidecar                        [door cities only]
    delay_events    event extraction batch             [door cities only]
    freeflow        late-night p5 per segment
    date_attrs      dow/season/pick/weather per date
    payloads        binned stats shards + segments GeoJSON
    distributions   per-segment delay-location files   [door cities only]
    areas           areas-of-interest rankings

Usage:
    PYTHONPATH=src uv run python analysis/network/run_pipeline.py --city mbta
    ... --stages traversals,freeflow      # subset (comma list)
    ... --from-stage registry             # resume from a stage onward
    ... --workers 8

Each stage is resumable by its own machinery (checkpoints / skip-existing),
so re-running the driver after a failure continues where it left off.
"""

from __future__ import annotations

import argparse
import json
import resource
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from dataio.cities import get_city  # noqa: E402

NET = "analysis/network"


def stage_commands(city, workers: int) -> list[tuple[str, list[str]]]:
    """Ordered (stage_name, argv) for this city."""
    c = city.city_id
    pbf = city.resolve(city.pbf_file) if city.pbf_file else None
    gtfs = city.resolve(city.gtfs_zip)
    stages: list[tuple[str, list[str]]] = [
        ("prefetch", ["uv", "run", "python", f"{NET}/prefetch.py", "--city", c]),
        ("way_match", [
            "uv", "run", "python", "record-a-ride/scripts/build_all_intersections.py",
            "--gtfs", str(gtfs), "--valhalla", city.valhalla_url,
            "--way-cache", str(city.resolve(city.way_cache_file)),
            "--out", str(city.resolve(city.intersections_file)),
            *((["--pbf", str(pbf)]) if pbf else []),
            *((["--exclude-route-prefix", ",".join(city.exclude_route_prefixes)])
              if city.exclude_route_prefixes else []),
        ]),
        # way_match runs both stages of build_all_intersections in one
        # invocation (stage 2 is minutes now that it reads the PBF); the
        # "intersections" stage is folded in and kept as an alias below.
        ("way_geoms", [
            "uv", "run", "python", f"{NET}/way_geometry.py", "--city", c,
            *((["--pbf", str(pbf)]) if pbf else []),
        ]),
        ("registry", ["uv", "run", "python", f"{NET}/registry.py", "--city", c]),
        ("traversals", [
            "uv", "run", "python", f"{NET}/run_reconstruct.py", "--city", c,
            "--workers", str(workers),
        ]),
    ]
    if city.has_door_data:
        stages += [
            ("door_join", ["uv", "run", "python", f"{NET}/door_join.py",
                           "--city", c, "--force"]),
            ("delay_events", ["uv", "run", "python", f"{NET}/delay_events.py",
                              "--city", c, "--workers", str(workers)]),
        ]
    stages += [
        ("freeflow", ["uv", "run", "python", f"{NET}/freeflow.py", "--city", c]),
        ("date_attrs", ["uv", "run", "python", f"{NET}/date_attrs.py", "--city", c]),
        ("payloads", ["uv", "run", "python", f"{NET}/build_payloads.py",
                      "--city", c, "--out",
                      str(REPO / "dashboard" / "data" / "network"
                          if c == "cta" else
                          REPO / "dashboard" / "data" / "network" / c)]),
    ]
    if city.has_door_data:
        stages.append(
            ("distributions", ["uv", "run", "python",
                               f"{NET}/build_distributions.py", "--city", c]))
    stages.append(
        ("areas", ["uv", "run", "python", f"{NET}/areas_of_interest.py",
                   "--city", c]))
    return stages


def run(city_id: str, only: set[str] | None, from_stage: str | None,
        workers: int) -> int:
    city = get_city(city_id)
    base = REPO / "outputs" / "network" / city.city_id
    log_dir = base / "pipeline_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    profile_path = base / "pipeline_profile.json"
    # Resume-friendly: keep prior invocations' successful stage records so
    # the profile covers the whole run, not just the last resume. A re-run
    # of a stage replaces its earlier record.
    if profile_path.exists():
        profile = json.loads(profile_path.read_text())
        profile["resumed_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    else:
        profile = {
            "city": city.city_id,
            "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "workers": workers,
            "has_door_data": city.has_door_data,
            "stages": [],
        }

    stages = stage_commands(city, workers)
    names = [s for s, _ in stages]
    if from_stage:
        if from_stage not in names:
            raise SystemExit(f"unknown --from-stage {from_stage}; stages: {names}")
        stages = stages[names.index(from_stage):]
    if only:
        unknown = only - set(names)
        if unknown:
            raise SystemExit(f"unknown stage(s) {sorted(unknown)}; stages: {names}")
        stages = [(s, cmd) for s, cmd in stages if s in only]

    print(f"pipeline[{city.city_id}]: {[s for s, _ in stages]}", flush=True)
    for name, cmd in stages:
        log_path = log_dir / f"{name}.log"
        r0 = resource.getrusage(resource.RUSAGE_CHILDREN)
        t0 = time.time()
        print(f"[{time.strftime('%H:%M:%S')}] ── {name} …", flush=True)
        with open(log_path, "a") as lf:
            lf.write(f"\n===== {time.strftime('%F %T')} {' '.join(cmd)}\n")
            lf.flush()
            proc = subprocess.run(cmd, cwd=REPO, stdout=lf, stderr=subprocess.STDOUT)
        wall = time.time() - t0
        r1 = resource.getrusage(resource.RUSAGE_CHILDREN)
        rec = {
            "stage": name,
            "cmd": " ".join(cmd),
            "wall_s": round(wall, 1),
            "cpu_user_s": round(r1.ru_utime - r0.ru_utime, 1),
            "cpu_sys_s": round(r1.ru_stime - r0.ru_stime, 1),
            # macOS reports ru_maxrss in bytes; cumulative max over children.
            "children_peak_rss_gb": round(r1.ru_maxrss / 1e9, 2),
            "exit": proc.returncode,
            "log": str(log_path.relative_to(REPO)),
        }
        profile["stages"] = [s for s in profile["stages"] if s["stage"] != name]
        profile["stages"].append(rec)
        profile["total_wall_s"] = round(
            sum(s["wall_s"] for s in profile["stages"]), 1)
        profile_path.write_text(json.dumps(profile, indent=1))
        status = "OK" if proc.returncode == 0 else f"FAIL exit={proc.returncode}"
        print(f"[{time.strftime('%H:%M:%S')}]    {name}: {status} "
              f"wall={wall:.0f}s cpu={rec['cpu_user_s'] + rec['cpu_sys_s']:.0f}s",
              flush=True)
        if proc.returncode != 0:
            print(f"PIPELINE-FAIL {name} (see {log_path})", flush=True)
            return 1
    print(f"PIPELINE-DONE {city.city_id} "
          f"total={profile['total_wall_s']:.0f}s → {profile_path}", flush=True)
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--city", required=True)
    ap.add_argument("--stages", default=None,
                    help="comma-separated subset of stages to run")
    ap.add_argument("--from-stage", default=None,
                    help="run this stage and everything after it")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()
    only = set(args.stages.split(",")) if args.stages else None
    sys.exit(run(args.city, only, args.from_stage, args.workers))


if __name__ == "__main__":
    main()
