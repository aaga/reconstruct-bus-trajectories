# Bus Trajectory Reconstruction & Delay Attribution

Reconstructs smooth, monotone, differentiable vehicle trajectories `f(t)` from
sparse AVL/GPS heartbeats using **LOCREG-PCHIP** (Huang et al., *Reconstructing
Transit Vehicle Trajectory Using High-Resolution GPS Data*, ITSC 2023), then
extends the method with an OSM-derived intersection layer and a **delay
decomposition** that attributes where each trip loses time — free-flow, signals,
dwell, crossings, and congestion.

The pipeline is **route-agnostic**: it consumes any GTFS route + a trace of
`(timestamp, lat, lon)` pings. The bundled study corridor — and all the
checked-in figures and dashboard aggregates — is **CTA Route 22 (Clark)
southbound**; retarget by editing [`src/corridor.py`](src/corridor.py) and
supplying that route's intersection cache (see *Retargeting* below).

![431 reconstructed trips — the CTA Route 22 SB study corridor](figures/C4_alltrips_aligned.png)

---

## What's reproduced vs. what's new

| Component                           | Source                | Status                              |
| ----------------------------------- | --------------------- | ----------------------------------- |
| Time / distance into trip from raw GPS | Huang et al. §II      | reproduced                          |
| LOCREG with tricube kernel          | Huang et al. §III-C   | reproduced (different bandwidth)    |
| PCHIP (Fritsch–Carlson)             | Huang et al. §III-B   | reproduced via `scipy.interpolate`  |
| LOCREG-PCHIP hybrid (Algorithm 1)   | Huang et al. §III-D   | reproduced                          |
| LOCREG-MQSI (C² variant)            | —                     | **new**: continuous-acceleration alternative |
| Map-matching                        | Huang et al. §II-B    | **replaced**: shape-snap onto the GTFS polyline instead of Valhalla per ping |
| Single-trip qualitative analysis    | Huang et al. §V       | reproduced (CTA data instead of MBTA Route 1) |
| Speed-at-door-open validation       | Huang et al. §IV-A    | **skipped**: CTA BusTime does not expose door state |
| OSM intersection enrichment         | —                     | **new**                             |
| Delay-decomposition / attribution   | —                     | **new**                             |
| Aggregation across hundreds of trips| —                     | **new**                             |

The biggest method deviation: where the paper uses `bandwidth = 20` on a 6 s
median heartbeat cadence, this uses `bandwidth = 5` because CTA BusTime publishes
positions every ~30 s. Both keep the LOCREG window at roughly two minutes of trip
time — the constant to revisit for a feed with a different cadence.

## Repository layout

Four layers — ① core logic, ② analysis, ③ visualization, ④ dashboard — plus the
standalone recording app. `core` is pure (no I/O, no plotting); `dataio` is the
I/O seam; `analysis/prep/` is the reuse seam shared by the figure scripts and the
dashboard builder.

```
src/                         ① importable packages (pythonpath=src; the tested core)
  ├─ core/                   pure business logic — no I/O, no plotting
  │    ├─ smooth.py          LOCREG, monotonicity, PCHIP + MQSI smoothers
  │    ├─ reconstruct.py     end-to-end trajectory reconstruction (pure)
  │    ├─ serialize.py       compact PCHIP (de)serialization
  │    ├─ control_points.py  the ControlPoint model + pure near-side classifier
  │    ├─ mapmatch/          projection onto the GTFS shape (+ Valhalla stub)
  │    └─ decompose/         signal-to-signal segmentation + per-segment delay buckets
  ├─ dataio/                 external I/O — gtfs.py (GTFS/AVL), realtime.py (R2
  │    │                     archive), intersections.py, way_match.py, vtrak.py
  │    ├─ records_io.py      disk/GTFS-backed wrappers (reconstruct_csv, build_segments)
  │    └─ sources/           pluggable GPS-trace adapters → one canonical trace
  ├─ corridor.py             the study route/pattern/shape in one place (edit to retarget)
  ├─ viz/                    matplotlib/plotly renderers + colour palette
  └─ cli/                    `bus-trajectories reconstruct | compare | build-*`

analysis/                    ② results & dashboard payloads
  ├─ run_decomposition.py    per-trip decomposition → trip_*.json + aggregate.csv
  ├─ build_dashboard_data.py unified dashboard payload builder (+ prep/ helpers)
  ├─ comparison.py           phone + R2 + AVL fusion → outputs/obs_trips/
  └─ data_prep/              scour the realtime archive → reconstruction bundles

figures/                     ③ visualization
  ├─ scripts/                all figure generators (smoothing / vtrak / decomposition)
  └─ <family>.png            rendered figures, families A1..H7 (see below)

dashboard/                   ④ one merged MapLibre + D3 dashboard
  ├─ app/  views/            Single trip (Trajectory · Speed) + Average trip (Overall · Segment)
  └─ data/                   catalog index.json + per-view payloads (trips + aggregates)
record-a-ride/               field-data collection web app + Cloudflare Pages API
tests/                       pytest suite (mirrors src/)
intersections_route22.json   OSM enrichment cache for the study shape (+ variants)
data/ outputs/ caches/       gitignored — regenerable inputs, bundles, and caches
docs/                        reference papers + attribution flowchart
```

Scripts run with `src` on the path (they insert it themselves); the console
entry point is `bus-trajectories` (`cli.cli:main`). Inputs and outputs live in
the gitignored `data/`, `outputs/`, and `caches/` folders.

## Retargeting to another route

Nothing in the pipeline hard-codes a route. `src/corridor.py` holds the one
study corridor — route id, GTFS pattern + shape, and a display name — and the
scripts read it (or take `--route` / `--pattern`). To point the toolkit at a
different route:

1. Edit `src/corridor.py` (`ROUTE_ID`, `PATTERN_ID`, `SHAPE_ID`, `CORRIDOR_NAME`).
2. Build that shape's intersection enrichment cache
   (`record-a-ride/scripts/build_all_intersections.py`) and set `INTERSECTIONS_FILE`.
3. Re-run the pipeline (reconstruct → free-flow baseline → decompose → figures /
   dashboard).

The checked-in figures and the dashboard's **Average trip** aggregate are
specific to the current corridor (CTA Route 22 SB); everything else is general.

### Figures

`figures/` holds the curated figure set, named by family (a letter) and
iteration (a number), each produced by the script noted:

| Family | Content | Script |
| ------ | ------- | ------ |
| `A1..A10` | archive → map-match pipeline walkthrough | `figures/scripts/build_smoothing.py` |
| `B1..B6`  | speed reconstruction | `figures/scripts/build_smoothing.py` |
| `C1..C5`  | time-space (50/100/all trips, multitrip) | `figures/scripts/build_{50trip,100trip,F4_from_bundle}.py` |
| `D1..D2`  | smoothing explainers (LOCREG, pipeline) | `figures/scripts/build_locreg_explainer.py`, `build_smoothing.py` |
| `E1`      | intersection map | `figures/scripts/build_smoothing.py` |
| `F1..F5`  | delay decomposition | `figures/scripts/build_decomposition.py`, `build_speed_profile.py` |
| `G1..G3`  | per-trip attribution (waterfall/bar/stem) | `figures/scripts/build_attribution.py` |
| `H1..H7`  | corridor-aggregate attribution | `figures/scripts/build_attribution.py` |

## Quickstart

```bash
uv sync                               # install runtime + dev deps
uv run pytest                         # run the test suite

# reconstruct from a CSV of pings (here: the study route/pattern) at bandwidth 5
PYTHONPATH=src uv run bus-trajectories reconstruct \
    your_pings.csv --gtfs data/gtfs/cta_gtfs.zip \
    --route 22 --pattern 3936 --bandwidth 5 --serialize --out outputs/out_bw5

# build the interactive bandwidth-comparison HTML over multiple bandwidths
PYTHONPATH=src uv run bus-trajectories compare \
    outputs/out_bw5 outputs/out_bw10 outputs/out_bw20 \
    --gtfs data/gtfs/cta_gtfs.zip --pattern 3936 --out compare.html
```

> All entry points run with `src` on the path — `pytest` and the console script
> pick it up automatically (pyproject `pythonpath` / the editable install), and the
> pipeline scripts insert it themselves. If a bare `uv run bus-trajectories` ever
> reports `No module named 'cli'` (uv can drop the flat-`src` editable path on an
> auto-sync), prefix with `PYTHONPATH=src` as shown, or run
> `uv sync --reinstall-package bus-trajectories`.

`cta_gtfs.zip` is downloaded on demand from the CTA's published GTFS feed the
first time a script needs it (into `data/gtfs/`); archived heartbeat data is
fetched lazily from a public Cloudflare R2 bucket (into `caches/realtime_archive/`)
using the paths in its `_manifest.parquet`.

## Data source — note on the scraper

The realtime ping archive is produced by a separate companion repository,
`scrape-bus-pings`, which polls several agencies (MBTA, MTA NYC Bus, TfL, CTA,
TransLink Vancouver) every 15 s, canonicalises every feed to a shared 26-column
schema, batches into 1-minute Parquet files, uploads to Cloudflare R2, and
compacts each completed UTC hour into a single Hive-partitioned object indexed
by a manifest. **That scraper is intentionally not included in this
repository** — it depends on agency API keys and an R2 bucket the analyst would
need to provide. This repo reads from its public R2 bucket
(`pub-777d0904efb449dc838791645b9e2e0f.r2.dev`), treating the archive as a
read-only data source.

## Algorithm in one paragraph

Given a sorted sequence of `(timestamp, latitude, longitude)` pings on a single
trip:

1. **Map-match**: project each `(lat, lon)` onto the GTFS shape polyline
   (`mapmatch.shape_snap.SnapToShapeMatcher`) to get a distance-into-trip
   value `d_i ∈ [0, L_route]` together with a perpendicular noise estimate.
2. **LOCREG**: at every ping `i`, fit a degree-3 polynomial in `x = t − t_i`
   to the `bandwidth = 5` nearest neighbours, weighted by the tricube
   kernel `w_k = (1 − |x_k/h|³)³`. Take `p(0) = a₀` as the smoothed value.
3. **Monotonise**: `x_i := max(x_i, x_{i-1})` (forward-fill).
4. **PCHIP**: build a piecewise-cubic Hermite spline through the cleaned
   `(t_i, x_i)` knots. The result is C¹, monotone, and made of cubics.
   (`smooth.locreg_mqsi` offers a C² quintic alternative with continuous
   acceleration.)
5. **Speed / acceleration**: `v(t) = f'(t)`, `a(t) = f''(t)` come for free.

## Delay attribution

The **chapter-3 decomposition** (`src/core/decompose/` + `figures/scripts/`)
follows Huang (2023), *Chapter 3 — Transit Delay Analysis*
(`docs/chapter 3 delay analysis.pdf`): signal-to-signal segmentation, per-segment
`T_obs = T_ff + T_dwell + D_signal + D_crossing + D_congestion` with `T_ff`
estimated as the 5th-percentile travel time of late-night (22:00–05:00 local)
trips on the same pattern. Produces the `F*` / `G*` / `H*` figure families and
the dashboard's Average-trip view.

Two deviations from the paper:
- AVL door-open/close data is not available, so dwell is attributed by
  proximity (`[x_stop - 30 m, x_stop + 10 m]`, clipped at intersection
  nodes). The `DwellAttributor` protocol leaves room to drop in an AVL-based
  attributor later without touching the rest of the package.
- Mid-block pedestrian signals count as signalized intersections for
  segmentation. A bus stop within 90 ft (~27.4 m) upstream of any signalized
  intersection is flagged as "near-side"; dwell attributed there is marked
  ambiguous because dwell-time vs. signal-delay can't be separated from GPS
  alone.

Run the decomposition end-to-end (on the current study corridor):

```bash
# 1. Build the late-night free-flow baseline (scours R2, reconstructs at bw=5,
#    writes p5 per segment).
PYTHONPATH=src uv run python analysis/data_prep/build_freeflow_baseline.py

# 2. Decompose every trip (or one with --trip-id <id>).
PYTHONPATH=src uv run python analysis/run_decomposition.py

# 3. Render figures.
PYTHONPATH=src uv run python figures/scripts/build_decomposition.py
```

## Sub-projects

- **`record-a-ride/`** — a mobile web app for collecting ground-truth field
  data (1 Hz GPS, accelerometer/gyro, hand-tagged delay events) to validate the
  reconstruction pipeline, plus a Python analysis side that compares phone
  tracks against the realtime archive. See [`record-a-ride/README.md`](record-a-ride/README.md).

## License

Code: MIT. The original Huang et al. paper PDF is not redistributed here.
Map data © OpenStreetMap contributors, available under the Open Database
License. Basemap tiles in figures are CartoDB Positron (No Labels) under
their respective terms.
