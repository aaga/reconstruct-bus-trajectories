# Network-wide segment-speed analysis

Full-network (all routes, all segments, all corridors) speed/delay analysis
for a city's bus network, driven by the R2 AVL archive. Chicago CTA is the
first city; everything city-specific lives in `src/dataio/cities.py`.

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

# 1. Segment registry + corridors (rebuild after any intersections-cache change)
PYTHONPATH=src uv run python analysis/network/registry.py --city cta
PYTHONPATH=src uv run python analysis/network/corridors.py --city cta

# 2. Prefetch archive hour-files (idempotent, concurrent)
PYTHONPATH=src uv run python analysis/network/prefetch.py --city cta

# 3. Batch reconstruction (resumable; skips existing (date,route) checkpoints)
PYTHONPATH=src uv run python analysis/network/run_reconstruct.py --city cta --workers 8
#    one date / one route for debugging:
#    ... run_reconstruct.py --city cta --date 2026-05-05 --route 22 --force

# 4. Free-flow + date attributes
PYTHONPATH=src uv run python analysis/network/freeflow.py --city cta
PYTHONPATH=src uv run python analysis/network/date_attrs.py --city cta \
    --start 2026-04-27 --end $(date +%F)

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
  `travel_time.last_times_at_boundaries`). Extend here (additive columns) when
  door open/close data lands (dwell vs inter-stop split).
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

## Adding a city

1. Add a `CityConfig` in `src/dataio/cities.py` (R2 agency name, tz, GTFS
   zip path, bandwidth for the feed's ping cadence, periods, picks, NOAA
   station).
2. Build the intersections + way caches for its GTFS shapes
   (`record-a-ride/scripts/build_all_intersections.py`, needs Valhalla).
3. Run the pipeline above with `--city <id>`.
