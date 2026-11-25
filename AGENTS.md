# AGENTS.md

Instructions for coding agents (and people) running the network pipeline in
this repository. Background and design decisions are in
[`README.md`](README.md) and
[`analysis/network/README.md`](analysis/network/README.md); this file is the
operating procedure.

## Ground rules

- Use `uv`. Run `uv sync` once, then `uv run ...` for everything. Scripts under
  `analysis/network/` put `src/` on the path themselves; for anything else
  prefix with `PYTHONPATH=src`.
- Run `uv run pytest` before and after code changes (about 2.5 minutes).
- `data/`, `caches/`, `outputs/` and `routing-valhalla/` are git-ignored and
  large. Never commit them. In a git worktree, symlink `data` and `caches` from
  the main checkout.
- Every stage is resumable: it skips work whose checkpoint already exists.
  Re-run a failed command instead of deleting outputs. Use `--force` only for
  the specific date or route you mean to redo.
- Long stages (reconstruction, delay events) take hours. Run them detached
  with output to a log file, and on macOS under `caffeinate -i`.
- All city-specific settings live in `src/dataio/cities.py`. Hand-curated
  overrides live in `src/dataio/exceptions.json`. Do not hard-code a city
  anywhere else.
- Every derived artifact embeds the sha256 of the city's `intersections.json`.
  If that cache is regenerated, rebuild everything from the `registry` stage.
- Do not commit API keys. The Transitland key is read from
  `$TRANSITLAND_API_KEY` or `caches/transitland.env`.

## Data sources

| Source | Where | Used by |
| --- | --- | --- |
| Realtime pings | Public R2 archive, <https://pub-777d0904efb449dc838791645b9e2e0f.r2.dev>, indexed by `_manifest.parquet` | every city except CTA |
| Corrected pings | `caches/pings/<city>/`, one Parquet per service date, written by `ping_ingest` | cities with `r2_sources` (`nyc`) |
| CTA pings | Local high-resolution AVL export (`avl_source_dir` in `cities.py`), not in the repo | `cta` |
| Timetable (GTFS) | `data/gtfs/<city>_gtfs.zip`; dated versions from Transitland under `caches/gtfs_history/<city>/` | all |
| Road network | An OpenStreetMap extract plus a Valhalla server built from the same extract, under `routing-valhalla/<area>/` | intersection and segment build |
| Weather | NOAA GHCN-D daily station (`noaa_station`) | `date_attrs` |
| Hourly rain and snow | NOAA MRMS radar rain crop and an ASOS station (`asos_station`) | `rain_mrms`, `weather_effect`, `weather_speed` (cities with `rain_mrms=True`) |

Archive objects are one Parquet file per agency per UTC hour:
`agency=<id>/year=YYYY/month=MM/day=DD/hour=HH.parquet`. All timestamps are
UTC. `src/dataio/realtime.py` is the only client; do not write another.

## Configured cities

| `--city` | Agency | Pings | Routing server | Notes |
| --- | --- | --- | --- | --- |
| `cta` | Chicago CTA | local AVL export, read in place | `localhost:8002` | needs the private export; has door data; 2024-01 onward |
| `mbta` | Boston MBTA | R2, agency `mbta` | `localhost:8003` | no door data |
| `translink` | Vancouver TransLink | R2, agency `translink` | `localhost:8004` | no door data, no weather |
| `nyc` | New York MTA buses | R2, agencies `mta-gtfs-rt` (to 2026-11-20) and `mta-siri` (after), repaired by `ping_ingest` | `localhost:8005` | no door data; hourly rain and snow effects; see NYC specifics |
| `cta-hf` | CTA 2 s test vehicles | local ingest | `localhost:8002` | investigation only, hidden from the dashboard |

`cta` cannot be run without the CTA export, whose path in `cities.py` is
specific to one machine. `mbta`, `translink` and `nyc` run from public data.

## Running an existing city

One driver runs every stage in order and writes a per-stage profile and logs
to `outputs/network/<city>/`:

```bash
uv run python analysis/network/run_pipeline.py --city mbta --workers 8
```

Useful variants:

```bash
# resume from a stage after a failure or a code change
uv run python analysis/network/run_pipeline.py --city mbta --from-stage traversals
# run a subset
uv run python analysis/network/run_pipeline.py --city mbta --stages freeflow,payloads
# debug one date and route
uv run python analysis/network/run_reconstruct.py --city mbta \
    --date 2026-09-15 --route 1 --force
```

Stage order (see the docstring of `analysis/network/run_pipeline.py`):

| Stage | Does | Needs |
| --- | --- | --- |
| `prefetch` | downloads the city's R2 hour files to `caches/realtime_archive/` | network |
| `way_match` | map-matches every GTFS shape and builds the signal list | Valhalla server, OSM extract |
| `signal_infer` | adds signals OSM misses and applies `signal_add` / `signal_reject` exceptions; does nothing unless the city sets `infer_signals_from_crossings` or has such exceptions | OSM extract |
| `way_geoms` | OSM way geometries | OSM extract |
| `registry` | signal-to-signal segments | intersections cache |
| `ping_ingest` | writes corrected per-date pings; does nothing unless the city has `r2_sources` | prefetched hours, dated timetables |
| `head_trim_check` | decides whether to trim pre-departure pings | cached pings |
| `traversals` | reconstructs every trip; one row per trip and segment | the slow stage |
| `door_join` | joins door events | door cities only |
| `delay_events` | slowdown events, trajectory speed, headway variability | traversals |
| `freeflow` | late-night 5th-percentile time per segment | traversals |
| `date_attrs` | day type, season, weather per date | NOAA |
| `payloads` | dashboard data under `dashboard/data/network/<city>/` | all above |
| `rain_mrms`, `weather_effect`, `weather_speed` | speed change in heavy rain and snow | cities with `rain_mrms=True` |
| `movements`, `ping_density`, `distributions`, `headway_cv`, `areas` | dashboard extras | payloads |

To pick up new days only, re-run `prefetch`, then
`--from-stage traversals` (`--from-stage ping_ingest` for `nyc`). Existing
date and route checkpoints are skipped.

To limit the date range, call the stage scripts directly: `prefetch.py`,
`run_reconstruct.py` and `delay_events.py` take `--start` and `--end`.

If the city has `gtfs_history_dir` set and the archive spans a timetable
change, refresh the dated timetables before `traversals`:

```bash
uv run python analysis/network/gtfs_history.py --city mbta \
    --start 2026-04-27 --end 2026-11-01
uv run python analysis/network/era_seg_bounds.py --city mbta --all-eras
```

View the result (the server must support range requests, so do not use
`python -m http.server`):

```bash
uv run python dashboard/serve.py --port 8931
```

Build the static public site with
`uv run python scripts/build_public_site.py --cities mbta,translink`.

### CTA specifics

`cta` skips `prefetch` (the export is read in place) and adds a history pass
and door data. Follow `analysis/network/README.md`, steps 2b and 4b, or run
`analysis/network/run_history.sh <start> <end>`.

### NYC specifics

- **Two archive agencies.** MTA positions were scraped from GTFS-rt until
  2026-11-20 (`mta-gtfs-rt`) and from SIRI afterwards (`mta-siri`).
  `r2_sources` in the `nyc` config lists both with their kind.
- **Never read the raw NYC hours directly.** GTFS-rt positions are not
  observations: the feed pushes the last real report along the timetable to
  the feed build time. `ping_ingest` reverses that, keeps one row per vehicle
  and timestamp, and regroups pings into vehicle runs because the feed's
  `trip_id` flips mid-trip. Every later stage reads `caches/pings/nyc/`.
  Rows whose trip is in no nearby timetable version are dropped.
- **`trip_id` in NYC outputs is a run id**, not the feed's trip id;
  `feed_trip_id` keeps the original.
- **Timetable.** MTA publishes six bus feeds. Merge them into
  `data/gtfs/nyc_gtfs.zip` with
  `PYTHONPATH=src uv run python src/dataio/gtfs_merge.py data/gtfs/nyc_gtfs.zip <six zips>`.
  The reversal needs the dated versions too, so run `gtfs_history.py --city
  nyc` and `era_seg_bounds.py --city nyc --all-eras` before `ping_ingest`.
- **Redo one date:**
  `uv run python analysis/network/ping_ingest.py --city nyc --date 2026-09-22 --force`,
  then `run_reconstruct.py` for the same date with `--force`.
- **Headway variability routes** were chosen with
  `analysis/network/cv_window.py --city nyc`; rerun it when the timetable
  changes and update `cv_routes`.

## Adding a new city from the R2 archive

1. **Check the archive has it.** Find the agency id and its date range:

   ```bash
   PYTHONPATH=src uv run python -c "
   from dataio import realtime
   m = realtime.load_manifest(refresh=True)
   print(m.groupby('agency').agg(hours=('path','size'), rows=('n_rows','sum')).sort_values('rows').tail(40))"
   ```

   Then load one hour with `realtime.load_hour(path)` and check the feed
   before trusting it: the median gap between pings per vehicle, whether
   `speed` is filled and in what unit, whether `trip_id` and `route_id` are
   filled, and whether timestamps advance (some feeds repeat a frozen
   timestamp). The manifest's `*_fill` columns give a first look.

   Also check whether positions are real observations. Feeds built with
   OneBusAway can publish timetable-extrapolated positions (MTA GTFS-rt
   does). Such a feed needs `r2_sources` with kind `oba_extrapolated` and a
   `ping_dir`, so that `ping_ingest` repairs it; see NYC specifics.

2. **Get the timetable.** Put the agency's current GTFS zip at
   `data/gtfs/<city>_gtfs.zip`. It must contain `shapes.txt`. If the archive
   spans a timetable change, add the agency's Transitland feed id to
   `FEED_OSID` in `analysis/network/gtfs_history.py` and set
   `gtfs_history_dir`.

3. **Stand up routing.** Download an OpenStreetMap extract covering every
   route (merge neighbouring regions if routes cross a border) into
   `routing-valhalla/<area>/`, build Valhalla tiles from that same file, and
   serve them on an unused port (8002 to 8005 are taken). The extract and the
   tiles must be the same vintage. The repo does not record the container
   command; any standard Valhalla server works.

4. **Add a `CityConfig`** in `src/dataio/cities.py` and register it in
   `CITIES`. Copy `_MBTA` or `_TRANSLINK` and set:

   - `city_id`, `r2_agency` (the manifest's agency id), `tz`
   - `gtfs_zip`, `intersections_file`, `way_cache_file` (under
     `caches/<city>/`), `archive_cache_dir`
   - `bandwidth`: keep the smoothing window near two minutes, so about
     `120 / median ping gap in seconds` (CTA at 30 s uses 5, MBTA at 16 s
     uses 9)
   - `periods`, `late_night`, and `late_night_wide` if overnight service is
     thin
   - `noaa_station` (empty string skips weather) and `holiday_region` (add
     the region to `HOLIDAYS_2026` in `analysis/network/date_attrs.py` if it
     is not `US` or `CA-BC`)
   - `pbf_file`, `valhalla_url`
   - `exclude_route_prefixes` for rail-replacement shuttles and similar
   - `has_door_data=False` unless you have a door or passenger-count extract
   - `cv_routes` only if you want headway variability for named routes
     (`analysis/network/cv_window.py` proposes a window and route set)
   - `r2_sources` and `ping_dir` only for a feed that needs repair before
     reconstruction
   - `feed_speed=True` only after checking the feed's speed field is real
     and in m/s
   - `monotone_min_move_m` if GPS jitter in dense streets breaks trips
     (default 30 m; `nyc` uses 50 m)
   - `rain_mrms=True` and `asos_station` for hourly rain and snow effects
     (United States only)

   City-config behaviour is tested in `tests/test_cities.py`; add a case
   there if the new city needs a rule the others do not.

5. **Smoke-test on one day before the full run.**

   ```bash
   uv run python analysis/network/prefetch.py --city <city> \
       --start 2026-10-20 --end 2026-10-21
   uv run python analysis/network/run_pipeline.py --city <city> \
       --stages way_match,way_geoms,registry,head_trim_check
   uv run python analysis/network/run_reconstruct.py --city <city> \
       --date 2026-10-20 --workers 4
   ```

   Read `outputs/network/<city>/traversals_index.jsonl`: it records, per date
   and route, how many trips were kept and why the rest were rejected. A high
   reject share usually means the wrong `max_perp_m`, a `route_id` mismatch
   between the feed and the timetable, or a service-day cutover problem.

6. **Run everything.**

   ```bash
   uv run python analysis/network/run_pipeline.py --city <city> --workers 8
   ```

7. **Show it in the dashboard.** Add the city to the map in
   `dashboard/app/network_data.js` (label and `../data/network/<city>` base),
   and to `PUBLIC_CITIES` in `scripts/build_public_site.py` if it should be on
   the public site.

8. **Record exceptions.** Wrong stop coordinates, signals that should be split
   or pinned, and terminal bays go in `src/dataio/exceptions.json` with what,
   why, evidence and date. Validate with
   `PYTHONPATH=src uv run python src/dataio/exceptions.py`.

## Things that go wrong

- **Joins on `shape_id` across timetable versions** silently drop rows. Key on
  segment ids (OSM node pairs), which do not change between versions.
- **Archive `trip_id` does not join GTFS `trips.txt`** for some feeds. Trips
  are assigned to shapes geometrically (`assign_shapes.py`); do not replace
  this with a join.
- **Regenerating the intersections cache** invalidates all outputs for that
  city.
- **Serving the dashboard without range support** makes the Explore tab
  re-download whole files on every query.
- **`No module named 'cli'`** from `uv run bus-trajectories`: prefix with
  `PYTHONPATH=src` or run `uv sync --reinstall-package bus-trajectories`.
