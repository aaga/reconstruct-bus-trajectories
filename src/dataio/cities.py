"""Per-city configuration for network-wide analysis.

``src/corridor.py`` holds the legacy single-corridor constants (Route 22 SB);
this module is its network-scale, multi-city successor. Everything the network
pipeline needs to retarget a new city lives in one :class:`CityConfig`:
paths to caches/GTFS, the R2 agency name, timezone, reconstruction bandwidth,
time-period definitions, schedule "picks", and the NOAA weather station.

Paths are repo-root-relative; resolve them with :meth:`CityConfig.resolve`
so entry points can run from any CWD (including git worktrees where
``caches/`` and ``data/`` are symlinks to the main checkout).
"""

from __future__ import annotations

from dataclasses import dataclass, replace as _dc_replace
from pathlib import Path

# Repo root = parent of src/. Mirrors how realtime.py resolves its cache dir.
_REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Pick:
    """One schedule pick (operator/schedule assignment period).

    ``start_date`` is the first service date (inclusive, ISO ``YYYY-MM-DD``);
    the pick ends the day before the next pick's start (the last configured
    pick is open-ended). ``gtfs_zip`` optionally points at the GTFS feed
    published for this pick; ``None`` means use the city default.
    """

    pick_id: str
    start_date: str
    gtfs_zip: str | None = None


@dataclass(frozen=True)
class CityConfig:
    city_id: str
    r2_agency: str  # agency value in the R2 archive manifest
    tz: str
    gtfs_zip: str  # repo-root-relative
    intersections_file: str
    way_cache_file: str
    archive_cache_dir: str
    bandwidth: int  # LOCREG bandwidth for this feed's ping cadence
    max_perp_m: float  # shape-snap off-route threshold
    service_day_cutover_h: int  # local hour where a service date rolls over
    # Ordered (name, start_hour, end_hour) in local time; end exclusive.
    # Wrapping periods (start > end) span midnight (e.g. late_night 22-6).
    periods: tuple[tuple[str, int, int], ...]
    late_night: tuple[int, int]  # free-flow window (start_h, end_h), wraps midnight
    picks: tuple[Pick, ...]
    noaa_station: str  # GHCN-D station id for daily weather
    deadhead_route_ids: tuple[str, ...] = ()
    # Widened fallback window for segments too thin in late_night (cities
    # with little overnight service — MBTA). None = no widening step.
    late_night_wide: tuple[int, int] | None = None
    # Route-id prefixes excluded from the network entirely (e.g. MBTA
    # "Shuttle-" rail replacements, filed as route_type 3 in GTFS).
    exclude_route_prefixes: tuple[str, ...] = ()
    # Door/APC (bus-state extract) availability. False => the pipeline skips
    # door_join + delay_events + distributions; payloads carry has_door=False
    # everywhere so the dashboard's door-derived families stay empty.
    has_door_data: bool = False
    # OSM extract the Valhalla tiles were built from (single-vintage rule);
    # consumed by build_all_intersections --pbf and way_geometry --pbf.
    pbf_file: str | None = None
    valhalla_url: str = "http://localhost:8002"
    # dw-row location anchor in delay_events: "raw" = door lat/lon snapped
    # to the shape (2026-08-05 default); "door_mid" = trajectory position at
    # the door-interval time-midpoint (cta-hf investigation).
    door_anchor: str = "raw"
    # Hidden from the dashboard city tabs (investigation-only cities).
    show_in_ui: bool = True

    def resolve(self, rel: str | Path) -> Path:
        """Resolve a repo-root-relative path (absolute paths pass through)."""
        p = Path(rel)
        return p if p.is_absolute() else _REPO_ROOT / p

    @property
    def periods_by_name(self) -> dict[str, tuple[int, int]]:
        return {name: (lo, hi) for name, lo, hi in self.periods}

    def period_for_hour(self, hour_local: int) -> str:
        """Map a local hour (0-23) to its period name."""
        for name, lo, hi in self.periods:
            if lo <= hi:
                if lo <= hour_local < hi:
                    return name
            elif hour_local >= lo or hour_local < hi:  # wraps midnight
                return name
        raise ValueError(f"hour {hour_local} not covered by periods for {self.city_id}")

    def pick_for_date(self, date_iso: str) -> str | None:
        """Last pick whose start_date <= date, or None before the first pick."""
        best: str | None = None
        for p in sorted(self.picks, key=lambda p: p.start_date):
            if p.start_date <= date_iso:
                best = p.pick_id
        return best


_CTA = CityConfig(
    city_id="cta",
    r2_agency="cta",
    tz="America/Chicago",
    gtfs_zip="data/gtfs/cta_gtfs.zip",
    intersections_file="caches/cta/intersections.json",
    way_cache_file="caches/cta/way_cache.json",
    archive_cache_dir="caches/realtime_archive",
    bandwidth=5,  # ~30 s AVL cadence
    max_perp_m=50.0,
    service_day_cutover_h=3,
    periods=(
        ("am_peak", 6, 10),
        ("midday", 10, 15),
        ("pm_peak", 15, 19),
        ("evening", 19, 22),
        ("late_night", 22, 6),
    ),
    late_night=(22, 5),
    # Confirmed via date_attrs.print_pick_report(): the Apr-2026 feed's main
    # service block (service_ids 678xx — "678" is CTA's pick counter, the same
    # prefix as shape_ids) runs 2026-04-15..2026-06-30, so pick 678
    # ("spring26") ends Jun 30 and pick 679 ("summer26") begins Jul 1.
    picks=(
        Pick("spring26", "2026-04-15"),
        Pick("summer26", "2026-07-01"),
    ),
    noaa_station="USW00094846",  # Chicago O'Hare GHCN-D
    deadhead_route_ids=("992",),
    has_door_data=True,
    pbf_file="routing-valhalla/illinois-260728.osm.pbf",
    valhalla_url="http://localhost:8002",
)

_MBTA = CityConfig(
    city_id="mbta",
    r2_agency="mbta",
    tz="America/New_York",
    gtfs_zip="data/gtfs/mbta_gtfs.zip",
    intersections_file="caches/mbta/intersections.json",
    way_cache_file="caches/mbta/way_cache.json",
    archive_cache_dir="caches/realtime_archive",
    bandwidth=9,  # ~16 s GTFS-RT cadence → ~144 s window (CTA: 5 × 30 s)
    max_perp_m=50.0,
    service_day_cutover_h=3,
    periods=(
        ("am_peak", 6, 10),
        ("midday", 10, 15),
        ("pm_peak", 15, 19),
        ("evening", 19, 22),
        ("late_night", 22, 6),
    ),
    late_night=(22, 5),
    late_night_wide=(20, 6),  # Boston sleeps 02-04; widen before class prior
    # MBTA "ratings" (their pick equivalent). The published feed only covers
    # Summer 2026 (feed_info: start 2026-07-21); archive dates before that
    # fall in the Spring rating. NB: spring-era trips are reconstructed
    # against the summer feed's shapes — routes changed by a bus-network-
    # redesign phase between ratings will reject on low_score for spring
    # dates (accepted simplification; watch reject stats).
    picks=(
        Pick("spring26", "2026-03-15"),
        Pick("summer26", "2026-07-21"),
    ),
    noaa_station="USW00014739",  # Boston Logan GHCN-D
    exclude_route_prefixes=("Shuttle",),
    has_door_data=False,  # no bus-state extract for MBTA
    pbf_file="routing-valhalla-ma/massachusetts-latest.osm.pbf",
    valhalla_url="http://localhost:8003",
)

# CTA-highfreq investigation (2026-08-05): 3 VTRAK vehicles at ~2 s cadence,
# ingested via analysis/network/highfreq_ingest.py into the shared archive
# cache under agency=cta-hf. Shares CTA's GTFS/registry/door data; dw rows
# anchor at the door-interval midpoint (per user decision for this stream).
_CTA_HF = _dc_replace(
    _CTA,
    city_id="cta-hf",
    r2_agency="cta-hf",
    door_anchor="door_mid",
    show_in_ui=False,
)

CITIES: dict[str, CityConfig] = {c.city_id: c for c in (_CTA, _MBTA, _CTA_HF)}


def get_city(city_id: str) -> CityConfig:
    try:
        return CITIES[city_id]
    except KeyError:
        raise KeyError(
            f"unknown city {city_id!r}; known: {sorted(CITIES)}"
        ) from None
