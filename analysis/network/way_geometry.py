"""Fetch OSM way geometries for every way in the way cache (one-time).

The registry's segment polylines are sliced from GTFS shape polylines, which
don't always hug the actual roadway. This fetches the real OSM way
geometries (``out geom``) for all way_ids referenced by
``caches/<city>/way_cache.json`` so the registry can draw segments along the
road itself.

Chunked, resumable (cache saved after every chunk), polite backoff. Follows
the Overpass conventions of ``dataio.intersections.query_overpass``.

Usage:
    PYTHONPATH=src uv run python analysis/network/way_geometry.py --city cta
Output:
    caches/<city_dir>/way_geoms.json    {way_id: [[lat, lon], ...]}
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from dataio.cities import get_city  # noqa: E402
from dataio.intersections import DEFAULT_OVERPASS_ENDPOINT  # noqa: E402

CHUNK = 400
UA = "reconstruct-bus-trajectories/1.0 (way geometry cache)"


def fetch_chunk(way_ids: list[int], endpoint: str, timeout_s: float = 120.0) -> dict[int, list]:
    ids_str = ",".join(str(i) for i in sorted(way_ids))
    query = f"[out:json][timeout:{int(timeout_s)}];way(id:{ids_str});out geom;"
    data = urllib.parse.urlencode({"data": query}).encode("utf-8")
    req = urllib.request.Request(endpoint, data=data, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout_s + 30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    out: dict[int, list] = {}
    for el in payload.get("elements", []):
        if el.get("type") == "way" and "geometry" in el:
            out[int(el["id"])] = [
                [round(p["lat"], 6), round(p["lon"], 6)] for p in el["geometry"]
            ]
    return out


def build(city_id: str, endpoint: str = DEFAULT_OVERPASS_ENDPOINT) -> Path:
    city = get_city(city_id)
    way_cache = json.loads(city.resolve(city.way_cache_file).read_text())
    out_path = city.resolve(city.way_cache_file).parent / "way_geoms.json"

    wanted = sorted({int(w["way_id"]) for spans in way_cache.values() for w in spans})
    geoms: dict[str, list] = (
        json.loads(out_path.read_text()) if out_path.exists() else {}
    )
    todo = [w for w in wanted if str(w) not in geoms]
    print(f"{len(wanted):,} ways referenced; {len(todo):,} to fetch")

    backoff = 5.0
    i = 0
    while i < len(todo):
        chunk = todo[i : i + CHUNK]
        try:
            got = fetch_chunk(chunk, endpoint)
        except Exception as e:  # noqa: BLE001 — rate limits, transient 5xx
            print(f"  chunk failed ({e}); backing off {backoff:.0f}s", flush=True)
            time.sleep(backoff)
            backoff = min(backoff * 2, 120.0)
            continue
        backoff = 5.0
        for wid, g in got.items():
            geoms[str(wid)] = g
        # Ways deleted/redacted upstream: record empty so we don't refetch.
        for wid in chunk:
            geoms.setdefault(str(wid), [])
        i += len(chunk)
        out_path.write_text(json.dumps(geoms))
        print(f"  [{i}/{len(todo)}] cached {len(got)} geometries", flush=True)
        time.sleep(1.0)  # politeness between chunks

    n_empty = sum(1 for g in geoms.values() if not g)
    print(f"done: {len(geoms):,} ways cached ({n_empty} missing upstream) → {out_path}")
    return out_path


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--city", default="cta")
    ap.add_argument("--endpoint", default=DEFAULT_OVERPASS_ENDPOINT)
    args = ap.parse_args()
    build(args.city, args.endpoint)


if __name__ == "__main__":
    main()
