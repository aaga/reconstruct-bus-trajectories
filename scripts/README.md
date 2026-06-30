# `scripts/` — build & figure-rendering pipeline

Convenience wrappers around the `bus_trajectories` package (in `../src`). They
fetch data from the public R2 archive, reconstruct trajectories, run the delay
decomposition, and render every figure in `../figures/` plus the interactive
dashboards. They are **not** imported by the package or its tests.

Run from the repo root. Most scripts need the package importable and read/write
the gitignored `data/`, `outputs/`, `caches/` folders:

```bash
PYTHONPATH=src uv run python scripts/<group>/<script>.py
```

## Groups

| Folder | What it does |
| ------ | ------------ |
| `data_prep/` | Scour the R2 archive and build the reconstruction bundles every figure depends on. `run_r2_route22_sb.py` (latest-N driver), `build_all_sb_trips.py` (→ `outputs/out_r2_bw5/trajectories.json`), `build_freeflow_baseline.py` (late-night p5 per-segment free-flow table). |
| `smoothing_figs/` | LOCREG-PCHIP smoothing & time-space figures. `build_slides.py` (pipeline walkthrough A/B/C/E figures), `build_50trip_timespace.py`, `build_100trip_aligned.py`, `build_F4_from_bundle.py`, `build_locreg_explainer.py`. |
| `vtrak/` | Dense-VTRAK (ROCKET) validation. `build_vtrak_smooth.py` is the shared helper the rest import from: `build_vtrak_speed.py`, `build_pchip_vs_mqsi.py` (PCHIP vs MQSI), `build_smoothing_dashboard.py`, `build_rocket_vs_r2.py`. Outputs are scratch PNGs (gitignored). |
| `decomposition/` | Chapter-3 delay decomposition figures. `run_decomposition.py` (decompose every trip → `outputs/out_decomposition/`), `build_decomposition_figs.py` (F family), `build_attribution_slides.py` (G/H families), `build_speed_profile_fig.py`. |
| `dashboard/` | Interactive MapLibre + D3 dashboards (tracked deliverable). `build_dashboard.py` (per-trip), `build_route_dashboard.py` (route-aggregate), shared `assets/`. |
| `plot_intersections.py` | Standalone Leaflet map of one shape's intersection ControlPoints. |

## Typical end-to-end order

```bash
# 1. Build the data bundles (slow; scours R2).
PYTHONPATH=src uv run python scripts/data_prep/build_all_sb_trips.py
PYTHONPATH=src uv run python scripts/data_prep/build_freeflow_baseline.py

# 2. Decompose every trip.
PYTHONPATH=src uv run python scripts/decomposition/run_decomposition.py

# 3. Render figures.
PYTHONPATH=src uv run python scripts/smoothing_figs/build_slides.py
PYTHONPATH=src uv run python scripts/decomposition/build_decomposition_figs.py
PYTHONPATH=src uv run python scripts/decomposition/build_attribution_slides.py
```

> Scripts that import a sibling (e.g. the `vtrak/` scripts import
> `build_vtrak_smooth`, `dashboard/build_route_dashboard` imports
> `build_dashboard`) rely on being run as a script so their own directory is on
> `sys.path` — run them by path as shown, not via `-m`.
