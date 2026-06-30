# Phone vs R2 trip-comparison analysis

Compares each phone-app observed trip against the same trip reconstructed from
the public R2 AVL archive, plus the observer's hand-tagged delays — on one
shared time axis.

```
build_comparison.py   pipeline: webapp + R2 -> reconstruct -> attribute -> data/*.json
r2_archive.py         fetch CTA AVL pings for one trip from the public R2 bucket
dashboard/            static d3 dashboard (open in a browser)
  index.html main.js style.css
  data/<key>.json     one payload per trip   (+ index.json)
```

## What it does

For every observed trip with dense, accurate phone GPS:

1. **Match** — pull the webapp pings/events/meta from the Pages backup API, and
   find the same trip in the R2 archive by `(route_id, vehicle_id, trip_id)`.
2. **Reconstruct both** on the *same* GTFS shape — phone @ `bw=20`, R2 @ `bw=5`
   (the repo's CTA convention) — via `bus_trajectories.pipeline.reconstruct_trip`.
3. **Attribute delays** on each reconstructed trajectory with the slowdown-window
   method (`detect_events`, v<5 mph) → each window labelled by the nearest
   stop/signal (dwell / dwell-near-signal / signal / crossing / congestion).
4. **Emit** `data/<key>.json` on a shared UTC wall-clock axis (seconds since a
   per-trip `t0`, rendered in Chicago local time), carrying both reconstructed
   trajectories, raw on-route pings, speed, inferred delays, and the webapp's
   recorded delays.

Two robustness features the trips forced:

- **Shape auto-selection.** The captured `pattern_id` is often the wrong
  variant or direction (e.g. rt20 recorded westbound 2063 while the bus ran
  eastbound). `choose_shape` map-matches the phone pings against every shape on
  the route and keeps the one with real forward progress — so the y-axis is the
  geometrically correct route distance.
- **`trip_id` reuse isolation.** BusTime reuses `trip_id`, sometimes same-day
  across both directions. The R2 ride is taken as the time-cluster overlapping
  the ride window; if that still collapses the reconstruction, it falls back to
  the strict boarding→alighting window.

## Run

```bash
# build payloads for all good-GPS trips (auto-selected), or pass explicit keys
python observation_tool/analysis/build_comparison.py --token ridethebus
python observation_tool/analysis/build_comparison.py --trips 1781534420859_1391

# view (local only — trip data incl. GPS traces stays on your machine)
cd observation_tool/analysis/dashboard && python -m http.server 8123
# open http://localhost:8123
```

Needs `cta_gtfs.zip` and `cta_intersections_all.json` at the repo root (both
already built). R2 downloads cache under the gitignored `r2_cache/`.

## Dashboard

- **Trajectory (time–space):** distance-along-route vs time. Phone (blue, bw=20)
  and R2 (magenta, bw=5) smoothed curves; per-source raw-ping toggles; optional
  faint stop lines. The phone curve starts where you boarded; R2 covers the
  bus's fuller run, so divergence/overlap is visible at a glance.
- **Speed & delays:** speed vs time for both sources, with three stacked delay
  rows — **web app** (top), **phone-GPS inferred** (middle), **R2 inferred**
  (bottom). Each row toggles independently and leaves a blank slot when off, so
  the three notions of "where the delay was" line up vertically for comparison.
- Both tabs share the time axis and are zoom/pan (scroll + drag); **reset**
  restores the full extent. Hover a delay bar for its cause, time and duration.

## Notes / limitations

- Only trips with dense phone GPS are built (coarse Precise-Location-off trips
  are skipped by `good_trips`).
- Inferred delays use v<5 mph windows ≥15 s; very brief stops below that floor
  won't appear as inferred bars even if hand-tagged.
- The shared axis is wall-clock; phone times are device-local (Chicago) and R2
  is UTC — both are converted to absolute epoch before alignment.
