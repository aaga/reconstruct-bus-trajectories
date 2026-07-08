"""Shared bits for the observation-tool build scripts: repo paths, sys.path
setup for the core/dataio packages, and on-demand GTFS download.

CTA ships a single GTFS zip. MTA Bus splits its schedule across the five NYCT
borough feeds plus the MTA Bus Company feed, so a city maps to a *list* of
(url, local-path) sources that ensure_city_gtfs() downloads and returns."""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
APP_ROOT = REPO / "record-a-ride"
GTFS_ZIP = REPO / "data" / "gtfs" / "cta_gtfs.zip"
GTFS_URL = "https://www.transitchicago.com/downloads/sch_data/google_transit.zip"

sys.path.insert(0, str(REPO / "src"))

# Per-city GTFS sources. MTA Bus = the five NYCT borough feeds + the MTA Bus
# Company feed, from the MTA S3 mirror. Zips live at the repo root (OUTSIDE the
# Pages deploy dir, so they're never served); a file already on disk is reused,
# not re-fetched. Each entry is (download URL, local zip path).
_MTA_BASE = "https://rrgtfsfeeds.s3.amazonaws.com"
GTFS_SOURCES: dict[str, list[tuple[str, Path]]] = {
    "cta": [(GTFS_URL, GTFS_ZIP)],
    "mta": [
        (f"{_MTA_BASE}/gtfs_bx.zip", REPO / "data" / "gtfs" / "gtfs_bx.zip"),      # Bronx
        (f"{_MTA_BASE}/gtfs_b.zip", REPO / "data" / "gtfs" / "gtfs_b.zip"),        # Brooklyn
        (f"{_MTA_BASE}/gtfs_m.zip", REPO / "data" / "gtfs" / "gtfs_m.zip"),        # Manhattan
        (f"{_MTA_BASE}/gtfs_q.zip", REPO / "data" / "gtfs" / "gtfs_q.zip"),        # Queens
        (f"{_MTA_BASE}/gtfs_si.zip", REPO / "data" / "gtfs" / "gtfs_si.zip"),      # Staten Island
        (f"{_MTA_BASE}/gtfs_busco.zip", REPO / "data" / "gtfs" / "gtfs_busco.zip"),  # MTA Bus Company
    ],
}


def _download(url: str, path: Path) -> Path:
    """Download `url` to `path` if it isn't already on disk."""
    if path.exists():
        return path
    print(f"[gtfs] downloading {url} -> {path.name}…")
    req = urllib.request.Request(
        url, headers={"User-Agent": "bus-trajectories/observation-tool"}
    )
    tmp = path.with_suffix(".part")
    with urllib.request.urlopen(req, timeout=300) as resp, open(tmp, "wb") as f:
        while chunk := resp.read(1 << 20):
            f.write(chunk)
    tmp.rename(path)
    print(f"[gtfs] saved {path.name}: {path.stat().st_size / 1e6:.1f} MB")
    return path


def ensure_gtfs(path: Path = GTFS_ZIP) -> Path:
    """Download the CTA GTFS zip if needed (back-compat for single-zip callers)."""
    return _download(GTFS_URL, path)


def ensure_city_gtfs(city: str) -> list[Path]:
    """Download all GTFS zips for a city, returning their local paths."""
    sources = GTFS_SOURCES.get(city)
    if not sources:
        raise SystemExit(f"unknown city {city!r}; known: {sorted(GTFS_SOURCES)}")
    return [_download(url, path) for url, path in sources]
