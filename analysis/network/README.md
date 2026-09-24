# Network-wide segment-speed analysis

Full-network (all routes, all segments, all corridors) speed/delay analysis
for a city's bus network. Chicago CTA is the first city; everything
city-specific lives in `src/dataio/cities.py`.

**CTA now runs off the local high-resolution AVL export, not the R2 scrape**
(2026-08-15), and since 2026-08-17 covers **2024-01-01 → 2026-08** — 957
service dates, ~3 billion pings. That history changes three things:

* **No prefetch/ingest step.** `avl_direct_read` reads the daily export
  parquet in place (`avl_source_dir`); materialising hour-files for 2.5
  years would cost ~45 GB for no benefit.
* **GTFS per service date.** Feed geometry genuinely moves — between the
  2023-12 and 2026-06 feeds, 58% of shared routes changed shape count and
  25% moved median shape length >10% — so `gtfs_history.py` caches every
  Transitland feed version covering the window and each date resolves to
  the one CTA was publishing that day. Segment ids stay era-independent
  (they are OSM node pairs), so a segment charts continuously across all
  2.5 years; only shape→segment mapping and stop positions are rebuilt.
* **Monthly stop registration.** The historical bus-state export carries no
  `stop_id`, and stops move over 2.5 years, so `monthly_stops.py`
  re-registers positions every month from door events.

⚠️ **Anything that joins on `shape_id` must span every era.** Historical
traversals carry that era's shape_ids (CTA re-prefixes them per feed
version), and these joins are inner: keying a lookup table off the
canonical registry alone silently drops or blanks all pre-2026 rows with no
error. This bit `segmap`, turn movements and ghost-zone adjacency. Prefer
keys derived from OSM node ids, which are era-invariant.

## Outputs

```
outputs/network/<city>/
├── segment_registry.json   canonical cross-route segments (OSM node-pair ids)
├── corridors.json          multi-route corridor chains (fwd/rev paired)
├── traversals/             per-(service_date, route) parquet checkpoints
│   └── service_date=YYYY-MM-DD/route=<id>.parquet
├── traversals_index.jsonl  per-unit stats + reject accounting
├── freeflow.json           per-segment p5 late-night free-flow (+fallbacks)
├── date_attrs.json         per-date dow/daytype/season/pick/weather
└── areas.json              ranked areas-of-interest contexts

dashboard/data/network/     payloads for the dashboard "Network" tab
├── meta.json  segments.json  corridors.json  areas.json  golden.json
└── stats_<period>.bin      packed columnar per-bin stats (5 shards)
```

## Pipeline (run in order)

```bash
# 0. One-time setup in a worktree: caches/ and data/ are gitignored, symlink
#    them from the main checkout (registry + archive cache live there):
ln -sfn <main-checkout>/caches caches
ln -sfn <main-checkout>/data data
#    and, if you want the Single/Average trip tabs working in the worktree's
#    dashboard, copy their gitignored payloads too:
cp <main-checkout>/dashboard/data/*.json dashboard/data/

# 1. Segment registry (rebuild after any intersections-cache change)
#    (corridors.py exists but is disabled/unwired for now — 2026-07 decision)
PYTHONPATH=src uv run python analysis/network/registry.py --city cta

# 2. Archive access
#    CTA: nothing to do — avl_direct_read reads the daily export in place.
#    Other cities still prefetch R2 hour-files (idempotent, concurrent):
PYTHONPATH=src uv run python analysis/network/prefetch.py --city mbta

# 2b. CTA history only: cache Transitland feed versions, map each era's
#     shapes onto the canonical segments, register stops per month.
PYTHONPATH=src uv run python analysis/network/gtfs_history.py --city cta \
    --start 2024-01-01 --end 2026-08-14      # key: caches/transitland.env
PYTHONPATH=src uv run python analysis/network/era_seg_bounds.py --city cta --all-eras
PYTHONPATH=src uv run python analysis/network/monthly_stops.py --city cta \
    --months 2024-01:2026-08
#     …or the whole historical pass, resumable, with a disk floor:
analysis/network/run_history.sh 2024-01-01 2026-08-14

# 3. Batch reconstruction (resumable; skips existing (date,route) checkpoints)
PYTHONPATH=src uv run python analysis/network/run_reconstruct.py --city cta --workers 8
#    one date / one route for debugging:
#    ... run_reconstruct.py --city cta --date 2026-05-05 --route 22 --force

# 4. Free-flow + date attributes
PYTHONPATH=src uv run python analysis/network/freeflow.py --city cta
PYTHONPATH=src uv run python analysis/network/date_attrs.py --city cta \
    --start 2024-01-01 --end $(date +%F)

# 4b. Door / APC data (optional but wanted: at-stop vs in-motion delay split)
#     One-time CSV → parquet conversion (source CSVs on slow cloud storage):
PYTHONPATH=src uv run python analysis/network/door_events.py --city cta "<csv>" ...
#     Verify timezone empirically, then build the per-traversal sidecar:
PYTHONPATH=src uv run python analysis/network/door_join.py --city cta --verify-tz
PYTHONPATH=src uv run python analysis/network/door_join.py --city cta

# 4c. Delay events (distribution viz + event-classified metrics; ~7 h @ 8
#     workers for the full archive — fold into the overnight batch)
PYTHONPATH=src uv run python analysis/network/delay_events.py --city cta --workers 8
PYTHONPATH=src uv run python analysis/network/build_distributions.py --city cta

# 5. Dashboard payloads + areas of interest
PYTHONPATH=src uv run python analysis/network/build_payloads.py --city cta
PYTHONPATH=src uv run python analysis/network/areas_of_interest.py --city cta
cp outputs/network/cta/areas.json dashboard/data/network/areas.json

# 6. Serve
cd dashboard && python3 -m http.server 8931   # open http://localhost:8931
```

## Key design points

- **Segments** are signal-to-signal spans keyed `SIG_<up>__SIG_<down>` by
  *canonical* OSM node ids. Two 2026-07 decisions (see `registry.py`
  docstring): boundaries are **traffic signals only** (ped signals stay in the
  cache for future attribution but don't split segments — "demote, don't
  delete"), and boundary nodes are **clustered globally at 30 m** so aliased
  OSM nodes (dual carriageways, stacked signal nodes) collapse to one
  intersection. Result: ≤1 segment per street span per direction, by
  construction. Direction is a property of (segment, route) — each route
  crosses a node pair one way — which is why the dashboard's direction filter
  only activates when a single route or corridor is selected.
- **Traversals stay legacy-keyed.** The 86-day batch predates canonicalization;
  `traversals_view.create_canonical_view()` merges constituent rows per trip
  (exact — boundary times are shared) so freeflow/payloads/AOI never need a
  re-run. If you DO re-run the batch, it still emits legacy ids and the view
  keeps working; regenerating the intersections cache, however, invalidates
  everything (sha256 guard).
- **Trip→shape assignment is geometric** (`assign_shapes.py`): archive
  trip_ids (BusTime tatripid) do NOT join GTFS trips.txt. Trips are matched
  against all candidate shapes of their route and scored
  `frac_on_route × frac_monotone`; short-turns tie-break to the shortest
  containing shape. This also survives GTFS pick changes: new-pick trips still
  match old-pick shape geometry unless the street routing itself changed.
- **Traversals** (`run_reconstruct.py`) are the core intermediate: one row per
  (trip, segment) with enter/exit times from full LOCREG-PCHIP reconstruction
  (Eq 3.3 "last time at x", vectorized in
  `travel_time.last_times_at_boundaries`).
- **Delay events** (`delay_events.py`, 2026-07-29 decisions): discrete
  slowdown events (<5 mph ≥15 s on a 2 s dense grid) classified against door
  cycles (`[open, open+dwell]`, verified empirically). Non-dwell = events
  with zero door overlap; dwell = union(door ∪ overlapping events) for EVERY
  door cycle (quick stops count); pax-weighted = nd events + >10 s pre/post
  shoulders × load as-of last door close. Neither bucket sums to overall
  delay (labeled "t_obs − t_ff"). Distribution viz buckets event locations
  into 10 ft bins upstream of the downstream signal (nd red / pre turquoise
  / post purple), events-or-seconds weighted.
- **Door/APC sidecar** (`door_events.py` + `door_join.py`, 2026-07): CTA
  bus-state-history door cycles (dwell seconds + rear/front ons/offs +
  passenger load; event types 3 and 5 are both real passenger service) joined
  to traversal windows by vehicle + instant. "Delay at stops" = door-open
  time only (user decision); approach/deceleration stays in-motion. Coverage
  is tracked per vehicle-day so true zero dwell ≠ missing data; timestamps
  verified Chicago-local against AVL windows (73% containment vs 38% under
  UTC). Bins carry n_door / sum_dwell / sum_delay_door / ons / offs (+
  passenger-load sums under the hood, not yet surfaced); door-metric
  denominators use door-covered dates only (`meta.door_date_counts`).
- **Free-flow** = p5 of late-night traversals pooled across routes per
  segment; thin segments fall back p10 → road-class prior (`freeflow.json`
  records the method per segment).
- **Aggregation** (`stats.py` + `build_payloads.py`): per
  (seg, route, pick, season, dow, weather, period) bin we store n / Σdelay /
  M2 / 16-bucket delay-ratio histogram. Means/variances merge exactly
  (Welford); medians/p90s come from summed histograms (±~4%). The JS decoder
  (`dashboard/app/network_data.js`) mirrors `stats.py`; `golden.json` keeps
  the two in lockstep (`selfTestGolden()` in the browser console).
- **AOI rankings** (`areas_of_interest.py`) use EXACT duckdb quantiles over
  raw traversals — never the histogram approximation. Priority =
  shrunk robust z × (1 + 0.5·log1p(buses/hour)); unweighted also emitted.
- **Pick boundaries** are configured in `cities.py` (CTA: service_id prefix
  "678" = pick number; spring26 = 2026-04-15, summer26 = 2026-07-01).
  Verify with `date_attrs.py --pick-report` whenever the GTFS zip updates.
- **seg_id stability**: every artifact embeds the sha256 of
  `intersections.json`; `build_payloads.py` refuses mismatched inputs. If the
  intersections cache is regenerated, rebuild EVERYTHING from step 1.

## Hard-coded exceptions

`src/dataio/exceptions.json` is the single committed home for hand-curated
overrides — stop-coordinate fixes, door-peak rejects, cluster splits,
segment pins, terminal-bay markers. Every entry records
what/why/evidence/date; the schema and per-type value contracts live in
`src/dataio/exceptions.py` (validates on load and standalone via
`PYTHONPATH=src uv run python src/dataio/exceptions.py`). Consumed by
`dataio.intersections` (cluster_split at per-shape clustering),
`analysis/network/registry.py` (cluster_split at global clustering;
terminal_stop / door_peak_reject / stop_coord_override at stop location).

## Querying (Explore tab)

`build_facts.py` emits a date-grain fact table for in-browser querying:

```
dashboard/data/network/facts/
  year=YYYY/month=MM/part-0.parquet   grain (seg, route, service_date,
                                      period, mvm, dir); measures are all
                                      ADDITIVE so a coarser slice is a SUM
  dim_segments.parquet   + n_near_side / n_far_side for side filtering
  dim_dates.parquet      dow, daytype, season, weather, pick
```

The dashboard's **Network → Explore** tab loads DuckDB-WASM lazily and runs
SQL against these over HTTP range requests, so a slice reads only the row
groups it touches instead of downloading the packed shards whole. Serve the
dashboard with a Range-capable server or every query refetches whole files:

```bash
uv run python dashboard/serve.py --port 8931     # NOT python -m http.server
```

## Adding a city

1. Add a `CityConfig` in `src/dataio/cities.py` (R2 agency name, tz, GTFS
   zip path, bandwidth for the feed's ping cadence, periods, picks, NOAA
   station).
2. Build the intersections + way caches for its GTFS shapes
   (`record-a-ride/scripts/build_all_intersections.py`, needs Valhalla).
3. Run the pipeline above with `--city <id>`.
