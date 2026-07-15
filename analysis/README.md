# analysis/ — results & dashboard payloads

Layer ② of the repo: turns reconstructed trajectories into delay-decomposition
results and the payloads the merged top-level `dashboard/` consumes. Depends on
`core` + `dataio`; never imported by `core`. Every script inserts `src` on the
path itself.

```
run_decomposition.py     per-trip delay decomposition → outputs/out_decomposition/{trip_<id>.json, aggregate.csv}
route_aggregate.py       route-aggregate payload (per-segment stacks + per-facility mean/p95) for the dashboard's Average-trip view
comparison.py            phone-app vs R2-archive vs AVL fusion for observed trips → outputs/obs_trips/*.json
build_dashboard_data.py  unified builder: obs_trips (trips) + route_aggregate (aggregate) → dashboard/data/
prep/geometry.py         pure route-polyline helpers (bearing, cumulative distance), shared with the figure scripts
data_prep/               scour the R2 archive → reconstruction bundles (free-flow baseline, all-SB trips)
```

## The corridor pipeline

1. **`data_prep/`** pulls trips from the public R2 archive and reconstructs them:
   `build_freeflow_baseline.py` writes the per-segment free-flow (late-night 5th-
   percentile) table; `build_all_sb_trips.py` writes the daytime bundle
   `outputs/out_r2_bw5/trajectories.json`.
2. **`run_decomposition.py`** decomposes every trip in the bundle into
   `T_ff + T_dwell + D_signal + D_crossing + D_congestion`, writing per-trip JSON
   and `aggregate.csv`.
3. **`route_aggregate.py`** rolls that into the route's per-segment stacks +
   per-facility mean/p95, and **`build_dashboard_data.py`** wraps it (plus the
   observed trips below) into `dashboard/data/`.

## Phone vs R2 comparison (`comparison.py`)

For every observed (Record-a-Ride) trip with dense, accurate GPS, compares the
phone track against the *same* trip reconstructed from the R2 AVL archive, plus
the observer's hand-tagged delays, on one shared time axis → one
`outputs/obs_trips/<key>.json` per trip (which `build_dashboard_data.py` turns
into the dashboard's Single-trip view).

1. **Match** — pull the webapp pings/events/meta from the Record-a-Ride Pages
   backup API, and find the same trip in the R2 archive by
   `(route_id, vehicle_id, trip_id)`.
2. **Reconstruct both** on the same GTFS shape — phone @ `bw=20`, R2 @ `bw=5` —
   via `core.reconstruct.reconstruct_trip`.
3. **Attribute delays** on each reconstruction with the slowdown-window method
   (`detect_events`, v<5 mph), each window labelled by the nearest stop/signal.
4. **Emit** the shared-axis payload: both trajectories, raw on-route pings,
   speed, inferred delays, and the webapp's recorded delays.

Two robustness features the real trips forced:

- **Shape auto-selection** (`choose_shape`) — the captured `pattern_id` is often
  the wrong variant or direction; this map-matches the phone pings against every
  shape on the route and keeps the one with real forward progress, so the y-axis
  is the geometrically correct route distance.
- **`trip_id` reuse isolation** — BusTime reuses `trip_id` (sometimes same-day
  across both directions); the R2 ride is taken as the time-cluster overlapping
  the ride window, falling back to the strict boarding→alighting window.

## Run

```bash
# 1. free-flow baseline + daytime bundle (scours R2; slow, resumable)
PYTHONPATH=src uv run python analysis/data_prep/build_freeflow_baseline.py
PYTHONPATH=src uv run python analysis/data_prep/build_all_sb_trips.py

# 2. decompose every trip → aggregate.csv + per-trip JSON
PYTHONPATH=src uv run python analysis/run_decomposition.py     # or --trip-id <id>

# 3. observed-trip payloads (needs the Pages-backup token)
PYTHONPATH=src uv run python analysis/comparison.py --token <SYNC_TOKEN>
#   or explicit keys:  --trips 1781534420859_1391

# 4. assemble the merged dashboard's data/
PYTHONPATH=src uv run python analysis/build_dashboard_data.py
```

Needs `data/gtfs/cta_gtfs.zip`, `caches/cta/intersections.json`, and (for
`comparison.py`) the CTA `bus_state_history` CSVs under `analysis/AVL data/` —
all gitignored, built or downloaded on demand. Realtime-archive hour files cache
under `caches/realtime_archive/`.

## Notes / limitations

- `comparison.py` only builds trips with dense phone GPS (coarse
  Precise-Location-off trips are skipped by `good_trips`).
- Inferred delays use v<5 mph windows ≥15 s; very brief stops below that floor
  won't appear as inferred bars even if hand-tagged.
- The shared axis is wall-clock: phone times are device-local (Chicago) and R2
  is UTC — both are converted to absolute epoch before alignment.
