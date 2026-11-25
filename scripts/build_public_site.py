"""Assemble the public, static copy of the dashboard.

Chicago's network payloads derive from CTA-internal AVL / bus-state exports
and stay private, so the public site carries only:

  * the app (index.html, style.css, app/) + a site.json that limits the
    Network tab to the chosen cities and drops Explore (CTA fact tables);
  * the Single-trip payloads listed in data/index.json (public by decision,
    2026-09-24);
  * each chosen city's network payloads — gzipped stats shards only (the
    browser inflates them; the raw .bin twins are a no-DecompressionStream
    fallback), plus the delay-location (dist*/) and headway-CV (cv/) files.

Excluded: data/network top level (= Chicago), facts/, vendor/duckdb (Explore
only), *.archive-* payload snapshots, serve.py, inspect_clusters.html.

Usage:
    uv run python scripts/build_public_site.py            # -> outputs/public_site
    uv run python scripts/build_public_site.py --cities mbta --out /tmp/site
"""
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DASH = REPO / "dashboard"
PUBLIC_CITIES = ("mbta", "translink")
# per-city payload entries the Network tab reads
CITY_FILES = ("meta.json", "segments.json", "golden.json")
CITY_DIRS = ("dist", "dist3mph", "cv")


def build(out: Path, cities: tuple[str, ...]) -> dict:
    if out.exists():
        shutil.rmtree(out)
    (out / "data" / "network").mkdir(parents=True)

    for name in ("index.html", "style.css"):
        shutil.copy2(DASH / name, out / name)
    shutil.copytree(DASH / "app", out / "app",
                    ignore=shutil.ignore_patterns(".DS_Store", "__pycache__"))
    (out / "site.json").write_text(json.dumps({
        "network_cities": list(cities),
        "default_city": cities[0],
        "explore": False,
    }, indent=1))

    # Single-trip payloads: exactly what the catalog lists (no aggregates —
    # the Average-trip tab is gone).
    idx = json.loads((DASH / "data" / "index.json").read_text())
    trips = [i for i in idx["items"] if i.get("kind") == "trip"]
    (out / "data" / "index.json").write_text(json.dumps({**idx, "items": trips}))
    for it in trips:
        shutil.copy2(DASH / "data" / f"{it['key']}.json",
                     out / "data" / f"{it['key']}.json")

    for c in cities:
        src = DASH / "data" / "network" / c
        dst = out / "data" / "network" / c
        dst.mkdir()
        for name in CITY_FILES:
            if (src / name).exists():
                shutil.copy2(src / name, dst / name)
        for gz in sorted(src.glob("stats_*.bin.gz")):
            shutil.copy2(gz, dst / gz.name)
        for d in CITY_DIRS:
            if (src / d).is_dir():
                shutil.copytree(src / d, dst / d,
                                ignore=shutil.ignore_patterns(".DS_Store"))

    files = [p for p in out.rglob("*") if p.is_file()]
    stats = {
        "out": str(out),
        "cities": list(cities),
        "n_trips": len(trips),
        "n_files": len(files),
        "total_mb": round(sum(p.stat().st_size for p in files) / 1e6, 1),
        "largest_mb": round(max(p.stat().st_size for p in files) / 1e6, 1),
    }
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(REPO / "outputs" / "public_site"))
    ap.add_argument("--cities", default=",".join(PUBLIC_CITIES),
                    help="comma list of NETWORK_CITIES keys (never cta)")
    a = ap.parse_args()
    cities = tuple(c for c in a.cities.split(",") if c)
    if "cta" in cities or "cta-hf" in cities:
        raise SystemExit("Chicago payloads are CTA-internal; not for the public site")
    print(json.dumps(build(Path(a.out), cities), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
