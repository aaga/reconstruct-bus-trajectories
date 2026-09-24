"""Per-city configuration for network-wide analysis.

``src/corridor.py`` holds the legacy single-corridor constants (Route 22 SB);
this module is its network-scale, multi-city successor. Everything the network
pipeline needs to retarget a new city lives in one :class:`CityConfig`:
paths to caches/GTFS, the R2 agency name, timezone, reconstruction bandwidth,
time-period definitions, and the NOAA weather station.

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
    noaa_station: str  # GHCN-D station id for daily weather ("" = skip weather)
    deadhead_route_ids: tuple[str, ...] = ()
    # Key into date_attrs.HOLIDAYS_2026 ("US" federal, "CA-BC" BC statutory).
    holiday_region: str = "US"
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
    # Historical GTFS era cache (analysis/network/gtfs_history.py): one zip
    # per Transitland feed version + index.json mapping date -> era.
    gtfs_history_dir: str | None = None
    # dw-row location anchor in delay_events: "raw" = door lat/lon snapped
    # to the shape (2026-08-05 default); "door_mid" = trajectory position at
    # the door-interval time-midpoint (cta-hf investigation).
    door_anchor: str = "raw"
    # Local high-resolution AVL export to ingest instead of the R2 scrape
    # (avl_ingest.py converts it into archive hour-files under r2_agency).
    # Speeds present -> VCHIP-ME reconstruction; absent -> plain PCHIP.
    avl_source_dir: str | None = None
    # Read avl_source_dir's daily parquet directly instead of ingesting it
    # into hour-files (2026-08-17): the 2.5-year archive is already date-
    # partitioned, so hour-files would cost ~45 GB for no benefit.
    avl_direct_read: bool = False
    # Monthly bus-state export covering the full history. Unlike the 3-month
    # cache it carries no stop_id/stop_sequence — harmless since door events
    # are re-attributed by location (delay_events, 2026-08-16) — and names
    # its dwell column dwell_time rather than dwell_s.
    door_source_dir: str | None = None
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


_CTA = CityConfig(
    city_id="cta",
    # 2026-08-15: CTA reads the redshift AVL export (ingested locally under
    # agency=cta-rs) instead of the R2 GTFS-rt scrape — denser pings + speeds.
    r2_agency="cta-rs",
    # 2026-08-17: the OneDrive archive supersedes the local redshift export —
    # same schema and units, but 958 days (2024-01-01 → 2026-08) instead of 93.
    avl_source_dir=(
        "/Users/ashwinagarwal/Library/CloudStorage/"
        "OneDrive-ChicagoTransitAuthority/CTA AVL Archive/avl_archive"),
    avl_direct_read=True,
    door_source_dir=(
        "/Users/ashwinagarwal/Library/CloudStorage/"
        "OneDrive-ChicagoTransitAuthority/Bus State History/bus_state_hist"),
    gtfs_history_dir="caches/gtfs_history/cta",
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
    noaa_station="USW00094846",  # Chicago O'Hare GHCN-D
    deadhead_route_ids=("992",),
    has_door_data=True,
    pbf_file="routing-valhalla/chicago/illinois-260728.osm.pbf",
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
    # NB: the published feed only covers Summer 2026 (feed_info: start
    # 2026-07-21); earlier archive dates are reconstructed against the summer
    # feed's shapes — routes changed by a bus-network-redesign phase between
    # ratings will reject on low_score for spring dates (accepted
    # simplification; watch reject stats).
    noaa_station="USW00014739",  # Boston Logan GHCN-D
    exclude_route_prefixes=("Shuttle",),
    has_door_data=False,  # no bus-state extract for MBTA
    pbf_file="routing-valhalla/boston/massachusetts-latest.osm.pbf",
    valhalla_url="http://localhost:8003",
)

_TRANSLINK = CityConfig(
    city_id="translink",
    r2_agency="translink",
    tz="America/Vancouver",
    gtfs_zip="data/gtfs/translink_gtfs.zip",
    intersections_file="caches/translink/intersections.json",
    way_cache_file="caches/translink/way_cache.json",
    archive_cache_dir="caches/realtime_archive",
    bandwidth=5,  # measured ~30 s deduped ping cadence (2026-08-26), same as CTA
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
    late_night_wide=(20, 6),  # NightBus thins 02-04; fallback for thin segments
    # Vancouver GHCN-D stations report no 2026 precipitation (verified
    # 2026-08-26: Harbour CS is temperature-only) — weather skipped for now.
    noaa_station="",
    holiday_region="CA-BC",
    has_door_data=False,  # no APC/door extract for TransLink
    pbf_file="routing-valhalla/bc/british-columbia-260825.osm.pbf",
    valhalla_url="http://localhost:8004",
    gtfs_history_dir="caches/gtfs_history/translink",
)

# CTA-highfreq investigation (2026-08-05): 3 VTRAK vehicles at ~2 s cadence,
# ingested via analysis/network/highfreq_ingest.py into the shared archive
# cache under agency=cta-hf. Shares CTA's GTFS/registry/door data; dw rows
# anchor at the door-interval midpoint (per user decision for this stream).
_CTA_HF = _dc_replace(
    _CTA,
    city_id="cta-hf",
    r2_agency="cta-hf",
    avl_source_dir=None,  # keeps its own R2 scrape; no redshift export
    door_anchor="door_mid",
    show_in_ui=False,
)

CITIES: dict[str, CityConfig] = {
    c.city_id: c for c in (_CTA, _MBTA, _TRANSLINK, _CTA_HF)
}


def get_city(city_id: str) -> CityConfig:
    try:
        return CITIES[city_id]
    except KeyError:
        raise KeyError(
            f"unknown city {city_id!r}; known: {sorted(CITIES)}"
        ) from None
