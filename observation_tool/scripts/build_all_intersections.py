"""Build the reusable all-CTA signalized-intersection dataset.

Runs the repo's existing two-stage pipeline over EVERY CTA bus shape
(~1300), where it has only ever been run for route 22's seven shapes:

  stage 1  way_match.match_shape   Valhalla map-snap, one call per shape
  stage 2  intersections           one Overpass fetch + per-shape walk

Both stages are wrapped for scale, without touching src/:

  * stage 1 is RESUMABLE — the way-cache JSON is checkpointed every few
    shapes and already-matched shapes are skipped on re-run;
  * stage 2 is CHUNKED — the stock build_intersections() issues a single
    Overpass query for all way_ids, which is fine for 7 shapes and fatal
    for all-CTA (tens of thousands of ways -> timeout/429). Here way_ids
    go up in batches, results are merged, and per-shape outputs are
    checkpointed so a crash resumes where it left off.

Output (same schema as intersections_route22.json, so
delay_decomposition.load_intersections consumes it unchanged):

    {shape_id: [{intersection_node_id, lat, lon, dist_along_route_m,
                 control_type, cross_street_names, ...}, ...]}

Usage (full run; expect hours, re-run freely to resume):

    python observation_tool/scripts/build_all_intersections.py \
        --valhalla http://localhost:8002

    # machinery check on a few shapes first:
    python observation_tool/scripts/build_all_intersections.py \
        --valhalla http://localhost:8002 --shape-ids 67803936,67803939
"""

from __future__ import annotations

import argparse
import json
import subprocess
import time
from dataclasses import asdict
from pathlib import Path

from gtfs_util import GTFS_ZIP, REPO, ensure_gtfs  # noqa: E402 (sets sys.path)

from dataio.intersections import (  # noqa: E402
    DEFAULT_OVERPASS_ENDPOINT,
    OverpassError,
    OverpassUnreachable,
    find_intersections_for_shape,
    query_overpass,
)
from dataio.gtfs import list_bus_shapes, load_gtfs_shape_with_dist  # noqa: E402
from dataio.way_match import (  # noqa: E402
    ValhallaUnreachable,
    load_cache,
    match_shape,
    save_cache,
)

DEFAULT_WAY_CACHE = REPO / "caches" / "cta" / "way_cache.json"
DEFAULT_OUT = REPO / "caches" / "cta" / "intersections.json"


# ---------------------------------------------------------------- stage 1


def stage1_way_cache(args, targets: list[str]) -> dict:
    """Valhalla-match every target shape; checkpoint + skip on re-run."""
    cache_path = Path(args.way_cache)
    cache = load_cache(cache_path) if cache_path.exists() else {}
    # An empty segment list means a previous run failed on that shape; retry.
    pending = [s for s in targets if not cache.get(s)]
    print(f"[stage1] {len(targets)} shapes; {len(targets) - len(pending)} cached, "
          f"{len(pending)} to match via {args.valhalla}")

    n_err = 0
    for i, sid in enumerate(pending, start=1):
        try:
            cache[sid] = match_shape(
                args.gtfs, sid, endpoint=args.valhalla, timeout_s=args.valhalla_timeout
            )
        except ValhallaUnreachable:
            save_cache(cache, cache_path)
            raise
        except Exception as e:  # noqa: BLE001 — per-shape failure must not kill the run
            cache[sid] = []
            n_err += 1
            print(f"[stage1]   {sid}: ERROR {e}")
        if i % args.checkpoint_every == 0 or i == len(pending):
            save_cache(cache, cache_path)
            print(f"[stage1]   [{i}/{len(pending)}] checkpointed ({n_err} errors)")
        time.sleep(args.valhalla_delay)

    save_cache(cache, cache_path)
    failed = [s for s in targets if not cache.get(s)]
    if failed:
        print(f"[stage1] WARNING: {len(failed)} shape(s) have no way-cache "
              f"(first few: {failed[:5]}); they will be skipped in stage 2")
    return cache


# ---------------------------------------------------------------- stage 2

# overpass-api.de drops connections from Python's TLS stack (fingerprint
# filtering — curl from the same machine is accepted, every ClientHello
# variant Python can produce is not). The curl transport below builds the
# SAME named-set query as intersections.query_overpass and shells out to
# curl; "auto" tries the stdlib client first and falls back permanently for
# the rest of the run.


def _overpass_ql(way_ids: list[int], timeout_s: float) -> str:
    """Duplicate of the query in intersections.query_overpass — keep in sync."""
    ids_str = ",".join(str(int(w)) for w in sorted(way_ids))
    return (
        f"[out:json][timeout:{int(timeout_s)}];\n"
        f"way(id:{ids_str}) -> .bus_ways;\n"
        f"node(w.bus_ways) -> .bus_nodes;\n"
        f"way(bn.bus_nodes) -> .all_ways;\n"
        f"node(w.all_ways) -> .all_nodes;\n"
        f"(.all_ways; .all_nodes;);\n"
        f"out body;\n"
    )


def query_overpass_curl(way_ids: list[int], *, endpoint: str,
                        timeout_s: float) -> dict:
    query = _overpass_ql(way_ids, timeout_s)
    result = subprocess.run(
        ["curl", "-sS", "--fail-with-body", "-m", str(int(timeout_s) + 30),
         "-A", "bus-trajectories/intersections (research)",
         "--data-urlencode", "data@-", endpoint],
        input=query.encode(), capture_output=True,
    )
    if result.returncode != 0:
        raise OverpassError(
            f"curl exit {result.returncode}: "
            f"{(result.stderr or result.stdout)[:300].decode(errors='replace')}"
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise OverpassError(f"Overpass returned non-JSON via curl ({e})") from e


def fetch_osm_chunked(way_ids: list[int], args) -> dict:
    """query_overpass() in batches, merging elements (dedup by type+id)."""
    chunks = [way_ids[i:i + args.chunk_size]
              for i in range(0, len(way_ids), args.chunk_size)]
    print(f"[stage2] Overpass: {len(way_ids)} ways in {len(chunks)} chunk(s) "
          f"of <= {args.chunk_size} via {args.overpass}")

    transport = args.transport
    seen: set[tuple[str, int]] = set()
    elements: list[dict] = []
    for ci, chunk in enumerate(chunks, start=1):
        for attempt in range(1, args.max_retries + 1):
            fetch = (query_overpass_curl if transport == "curl"
                     else query_overpass)
            try:
                osm = fetch(chunk, endpoint=args.overpass,
                            timeout_s=args.overpass_timeout)
                break
            except OverpassError as e:
                if transport == "auto" and isinstance(e, OverpassUnreachable):
                    print(f"[stage2]   chunk {ci}: urllib unreachable "
                          f"({e}) — switching to curl transport")
                    transport = "curl"
                    continue
                if attempt == args.max_retries:
                    raise
                wait = args.overpass_delay * 2 ** attempt
                print(f"[stage2]   chunk {ci}: {e} — retry {attempt}/"
                      f"{args.max_retries - 1} in {wait:.0f}s")
                time.sleep(wait)
        n_new = 0
        for el in osm.get("elements") or []:
            ident = (el.get("type"), el.get("id"))
            if ident in seen:
                continue
            seen.add(ident)
            elements.append(el)
            n_new += 1
        print(f"[stage2]   chunk {ci}/{len(chunks)}: +{n_new} elements "
              f"({len(elements)} total)")
        time.sleep(args.overpass_delay)
    return {"elements": elements}


def stage2_intersections(args, targets: list[str], way_cache: dict) -> None:
    out_path = Path(args.out)
    existing: dict[str, list] = (
        json.loads(out_path.read_text()) if out_path.exists() else {}
    )
    pending = [s for s in targets if s not in existing and way_cache.get(s)]
    print(f"[stage2] {len(targets)} shapes; {len(existing)} already built, "
          f"{len(pending)} to enrich")
    if not pending:
        return

    all_way_ids = sorted({seg.way_id for sid in pending for seg in way_cache[sid]})
    osm = fetch_osm_chunked(all_way_ids, args)

    def checkpoint():
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(existing))

    for i, sid in enumerate(pending, start=1):
        try:
            polyline, dist = load_gtfs_shape_with_dist(args.gtfs, sid)
            cps = find_intersections_for_shape(way_cache[sid], polyline, dist, osm)
            existing[sid] = [asdict(c) for c in cps]
            n_sig = sum(c.control_type in ("traffic_signals", "ped_crossing_signal")
                        for c in cps)
            print(f"[stage2]   [{i}/{len(pending)}] {sid}: {len(cps)} control "
                  f"points ({n_sig} signalized)")
        except Exception as e:  # noqa: BLE001
            print(f"[stage2]   [{i}/{len(pending)}] {sid}: ERROR {e}")
        if i % args.checkpoint_every == 0 or i == len(pending):
            checkpoint()

    checkpoint()
    print(f"[stage2] wrote {out_path} ({out_path.stat().st_size / 1e6:.1f} MB, "
          f"{len(existing)} shapes)")


# ------------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--gtfs", type=Path, default=GTFS_ZIP,
                    help="CTA GTFS zip (downloaded if missing)")
    ap.add_argument("--valhalla", default="http://localhost:8002",
                    help="Valhalla service base URL")
    ap.add_argument("--overpass", default=DEFAULT_OVERPASS_ENDPOINT,
                    help="Overpass API endpoint")
    ap.add_argument("--way-cache", default=DEFAULT_WAY_CACHE,
                    help=f"stage-1 checkpoint JSON (default {DEFAULT_WAY_CACHE})")
    ap.add_argument("--out", default=DEFAULT_OUT,
                    help=f"output JSON (default {DEFAULT_OUT})")
    ap.add_argument("--shape-ids", default=None,
                    help="comma-separated subset (default: all bus shapes)")
    ap.add_argument("--chunk-size", type=int, default=300,
                    help="way_ids per Overpass query (default 300)")
    ap.add_argument("--checkpoint-every", type=int, default=25,
                    help="shapes between checkpoint writes (default 25)")
    ap.add_argument("--valhalla-delay", type=float, default=0.1,
                    help="seconds between Valhalla calls (default 0.1)")
    ap.add_argument("--valhalla-timeout", type=float, default=120.0)
    ap.add_argument("--overpass-delay", type=float, default=3.0,
                    help="seconds between Overpass chunks (default 3)")
    ap.add_argument("--overpass-timeout", type=float, default=180.0)
    ap.add_argument("--max-retries", type=int, default=4,
                    help="Overpass attempts per chunk (default 4)")
    ap.add_argument("--transport", choices=["auto", "urllib", "curl"],
                    default="auto",
                    help="Overpass HTTP client; 'auto' falls back to curl "
                         "when the stdlib client is TLS-fingerprint-blocked")
    ap.add_argument("--skip-stage1", action="store_true",
                    help="use the existing way-cache as-is")
    args = ap.parse_args()

    ensure_gtfs(args.gtfs)
    targets = (args.shape_ids.split(",") if args.shape_ids
               else list_bus_shapes(args.gtfs))

    if args.skip_stage1:
        way_cache = load_cache(args.way_cache)
    else:
        way_cache = stage1_way_cache(args, targets)
    stage2_intersections(args, targets, way_cache)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
