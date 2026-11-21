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
    # Read the v2 R2 scrape's `speed` column (m/s, scraper-normalised) as
    # speed_mps for VCHIP-ME. Off by default so cities whose feeds carry
    # sparse/unchecked speeds keep their plain-PCHIP reconstructions.
    feed_speed: bool = False
    # Minimum fraction of a trip's pings with a usable speed for VCHIP-ME
    # (fit_trajectory gate); below it the trip falls back to plain PCHIP.
    # 0 = VCHIP-ME whenever any speed is present. RIPTA: 0, because its
    # feed omits speed exactly when the bus is at 0 mph, so low fill marks
    # dwell-heavy trips, not bad data (hold-out test 2026-11-02:
    # VCHIP-ME beats PCHIP on sub-50% trips too, 31.4 vs 34.1 m MAE).
    speed_min_fill: float = 0.5
    # Promote uncontrolled junctions to signals when OSM tags >= 2
    # signalized crosswalks around them on different legs but no
    # highway=traffic_signals node (analysis/network/signal_infer.py).
    infer_signals_from_crossings: bool = False
    # Monthly bus-state export covering the full history. Unlike the 3-month
    # cache it carries no stop_id/stop_sequence — harmless since door events
    # are re-attributed by location (delay_events, 2026-08-16) — and names
    # its dwell column dwell_time rather than dwell_s.
    door_source_dir: str | None = None
    # Hidden from the dashboard city tabs (investigation-only cities).
    show_in_ui: bool = True
    # Monotonicity gate jitter floor (m): along-shape moves smaller than this
    # are GPS noise, not reversals. Dense street canyons need more: on NYC
    # at ~30 s cadence a crawling bus moves a few tens of metres per ping,
    # so 30-50 m wobble failed 7.6% of trips; a 50 m floor recovers 83% of
    # them and changes none of the kept traces (2026-09-29).
    monotone_min_move_m: float = 30.0
    # Weather speed effects (weather_effect.py, 2026-10-05): heavy rain from
    # NOAA MRMS hourly gauge-corrected precipitation (US only, rain_mrms.py)
    # and snow hours from an airport's hourly METAR present-weather codes.
    rain_mrms: bool = False
    asos_station: str = ""  # IEM ASOS id for snow hours, e.g. "ORD"
    # Headway CV (delay_events --headway-cv -> build_cv): which routes get
    # the per-10 ft headway moments, from which service date, and within
    # which local-hour windows [lo, hi) both crossings must fall. Empty
    # routes = no CV for this city. Kept per city because the frequent
    # network and its operating hours differ by agency (2026-09-24).
    cv_routes: tuple[str, ...] = ()
    cv_start: str | None = None  # None = whole archive
    cv_hours_weekday: tuple[int, int] = (6, 21)
    cv_hours_weekend: tuple[int, int] = (9, 21)
    # Day types (date_attrs.daytype_of: weekday / sat / sun / holiday, by
    # holiday_region — the dashboard's daytype filter) whose dates count.
    cv_daytypes: tuple[str, ...] = ("weekday", "sat", "sun", "holiday")
    # Archive split by source kind (analysis/network/ping_ingest.py,
    # 2026-11-20): ((r2 agency, kind), ...). Kind "oba_extrapolated" marks
    # feeds whose positions are pushed along the timetable to feed-build
    # time (MTA GTFS-rt); those rows get the timetable reversal. Kind
    # "observed" rows (MTA SIRI) are real reports and pass through. A city
    # with r2_sources is read by every batch from ``ping_dir`` (one
    # corrected parquet per service date), never from the raw hour-files.
    r2_sources: tuple[tuple[str, str], ...] = ()
    ping_dir: str | None = None

    @property
    def r2_agencies(self) -> tuple[str, ...]:
        """Every R2 agency prefetch must pull for this city."""
        return tuple(a for a, _ in self.r2_sources) or (self.r2_agency,)

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
    rain_mrms=True,
    asos_station="ORD",
    # CTA frequent network (moved from delay_events.CV_ROUTES 2026-09-24)
    cv_routes=("9", "12", "81", "72", "4", "J14", "20", "34", "47", "49",
               "53", "54", "55", "60", "63", "66", "77", "79", "82", "95"),
    cv_start="2026-01-01",
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
    # Canonical geometry = the current (Fall 2026) feed; earlier archive
    # dates resolve to the Transitland era MBTA was publishing that day
    # (gtfs_history + era_seg_bounds), so spring/summer rating shapes still
    # map onto the canonical segments (2026-09-24 rebuild).
    noaa_station="USW00014739",  # Boston Logan GHCN-D
    exclude_route_prefixes=("Shuttle",),
    has_door_data=False,  # no bus-state extract for MBTA
    # Frequent-route rule (as Prague/Rome/Seattle; 2026-11-12): both
    # directions >= 4 scheduled departures in every hour 10-15 at the
    # reference stop on all 104 non-holiday weekdays Apr 27 - Sep 23 (each
    # judged against its own GTFS era, cv_window.qualifying), AND median
    # observed weekday trips >= 4 every hour, both directions. Schedule-only
    # passes dropped: 71, 73 (one hour at 3 observed), 742 (SL2: no traversals).
    cv_routes=("1", "9", "15", "16", "22", "23", "28", "31", "32", "39", "57",
               "66", "77", "104", "109", "110", "111", "116", "741", "743",
               "749"),
    # non-holiday weekday middays only (user decision 2026-09-24)
    cv_hours_weekday=(10, 15),
    cv_daytypes=("weekday",),
    pbf_file="routing-valhalla/boston/massachusetts-260923.osm.pbf",
    valhalla_url="http://localhost:8003",
    gtfs_history_dir="caches/gtfs_history/mbta",
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
    # Frequent-route rule (as Prague/Rome/Seattle/MBTA; 2026-11-12): both
    # directions >= 4 scheduled departures in every hour 10-15 at the
    # reference stop on all 45 non-holiday weekdays Jun 23 - Aug 26 (each
    # judged against its own GTFS era, cv_window.qualifying), AND median
    # observed weekday trips >= 4 every hour, both directions. Schedule-only
    # passes dropped (an hour at <= 3 observed): 022, 028, 068, 103, 351,
    # 503. route_ids are TransLink internal ids (short names in comments).
    cv_routes=(
        "6612", "6613", "6614", "6615", "6616", "6619", "6620",  # 002-010
        "6622", "6623", "6624", "30055", "11201", "6633",  # 016,017,019,023,033,041
        "6636", "6638", "6641", "6642", "46099", "6645",  # 049,084,099,100,102,106
        "32799", "6649", "6651", "6657", "6658", "6667",  # 119,123,130,144,145,160
        "6682", "39307", "6687", "6704", "6705", "6821",  # 240,245,250,320,321,322
        "6706", "17707", "6722", "34542", "34543",  # 323,337,401,406,408
        "37808", "38311", "37809", "37810", "37807", "46604",  # R1-R6
    ),
    cv_start="2026-06-24",  # archive starts 12:00 PDT Jun 23 (partial window)
    cv_hours_weekday=(10, 15),
    cv_daytypes=("weekday",),
    pbf_file="routing-valhalla/bc/british-columbia-260825.osm.pbf",
    valhalla_url="http://localhost:8004",
    gtfs_history_dir="caches/gtfs_history/translink",
)

_NYC = CityConfig(
    city_id="nyc",
    # MTA NYC Transit + MTA Bus Co. The archive is split by feed
    # (2026-11-20): GTFS-rt vehiclePositions to 2026-11-20 (positions are
    # timetable extrapolations, reversed at ingest), SIRI VehicleMonitoring
    # after (real reports).
    r2_agency="mta-gtfs-rt",
    r2_sources=(("mta-gtfs-rt", "oba_extrapolated"), ("mta-siri", "observed")),
    ping_dir="caches/pings/nyc",
    tz="America/New_York",
    # MTA publishes six bus GTFS feeds (five boroughs + MTA Bus Co.);
    # scripts/merge_nyc_gtfs.py merges them into this one zip.
    gtfs_zip="data/gtfs/nyc_gtfs.zip",
    intersections_file="caches/nyc/intersections.json",
    way_cache_file="caches/nyc/way_cache.json",
    archive_cache_dir="caches/realtime_archive",
    bandwidth=5,  # measured ~28 s median ping cadence (2026-09-29), like CTA
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
    late_night_wide=(20, 6),
    noaa_station="USW00094728",  # NY Central Park GHCN-D
    has_door_data=False,
    pbf_file="routing-valhalla/nyc/new-york-260928.osm.pbf",
    valhalla_url="http://localhost:8005",
    gtfs_history_dir="caches/gtfs_history/nyc",
    monotone_min_move_m=50.0,  # Manhattan/Queens GPS jitter (see field doc)
    rain_mrms=True,
    asos_station="NYC",  # Central Park ASOS (same station as daily weather)
    # Headway CV (2026-09-29): weekday 10-15 = the dashboard midday period,
    # chosen by analysis/network/cv_window.py from the static GTFS. It holds
    # the most 15-min-or-better routes of any window >= 5 h (156 of 338 on
    # the Fall feed; peaks reach ~200 but only for ~2 h). Routes = the 155
    # that qualify (>= 4 departures every hour, both directions) in EVERY
    # GTFS era of the archive, so each is frequent throughout.
    cv_routes=(
        "B1", "B3", "B6", "B8", "B9", "B11", "B12", "B14", "B15", "B17", "B20",
        "B25", "B26", "B35", "B36", "B38", "B41", "B42", "B44", "B44+", "B45",
        "B46", "B46+", "B49", "B52", "B54", "B60", "B61", "B63", "B65", "B68",
        "B82", "B82+", "B83", "B100", "B103", "BX1", "BX2", "BX3", "BX4A",
        "BX5", "BX6", "BX6+", "BX7", "BX9", "BX10", "BX11", "BX12", "BX12+",
        "BX13", "BX15", "BX17", "BX19", "BX21", "BX22", "BX27", "BX28", "BX30",
        "BX31", "BX32", "BX35", "BX36", "BX38", "BX39", "BX41", "BX41+", "M1",
        "M2", "M3", "M4", "M5", "M7", "M9", "M11", "M14A+", "M14D+", "M15",
        "M15+", "M23+", "M31", "M34+", "M34A+", "M35", "M42", "M57", "M60+",
        "M66", "M72", "M79+", "M86+", "M96", "M100", "M101", "M102", "M103",
        "M104", "M116", "M125", "Q1", "Q3", "Q4", "Q5", "Q06", "Q08", "Q09",
        "Q10", "Q11", "Q12", "Q13", "Q14", "Q17", "Q20", "Q22", "Q23", "Q24",
        "Q25", "Q26", "Q27", "Q28", "Q32", "Q33", "Q35", "Q40", "Q43", "Q44+",
        "Q45", "Q46", "Q53+", "Q54", "Q55", "Q56", "Q58", "Q60", "Q61", "Q64",
        "Q65", "Q69", "Q74", "Q75", "Q80", "Q83", "Q84", "Q85", "Q88", "Q98",
        "Q100", "Q110", "Q112", "S46", "S48", "S53", "S62", "S78", "S79+", "S93",
    ),
    cv_hours_weekday=(10, 15),
    cv_daytypes=("weekday",),
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
    rain_mrms=False,  # investigation stream: no weather-effect stage
    asos_station="",
)

CITIES: dict[str, CityConfig] = {
    c.city_id: c for c in (_CTA, _MBTA, _TRANSLINK, _NYC, _CTA_HF)
}


def get_city(city_id: str) -> CityConfig:
    try:
        return CITIES[city_id]
    except KeyError:
        raise KeyError(
            f"unknown city {city_id!r}; known: {sorted(CITIES)}"
        ) from None
