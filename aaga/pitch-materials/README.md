# Pitch materials: "Half a Mile in the Bronx Shows How to Fix New York's Buses"

Code and data behind the final opinion-pitch mock-up (NYC bus delay, 27 April to 21 November 2026).
The built page is `nyt_research/pages/pitch_f.html`.

## Layout

- `nyt_research/` : the citywide analysis.
  - `eng.py` : delay engine (10 ft delay curve per route shape, smoothed 5th-percentile baseline = other traffic, every peak above it credited to a stop or light).
  - `cube.py`, `cube2.py`, `run_city.py`, `period.py`, `jobs.py`, `rerun1.sh`, `rerun2.sh`, `mk_x.py` : build the intermediate tables from `outputs/network/nyc`.
  - `tsp.py`, `tsp_an.py`, `ace.py`, `wc.py`, `wc_all.py`, `street.py`, `twins.py`, `bundle.py`, `aligned.py`, `events.py`, `rush.py`, `bap2.py`, `rush_recs.py` : the individual analyses (signal priority, cameras, 42nd Street, plan corridors, street ranking).
  - `figdata*.py` : figure data (`fig*.json`). `figdata5.py` is the 42nd Street noon-to-8pm split by cause.
  - `pages/build_f.py` : builds the final page. `pages/fdata.py` prepares East Tremont Av (before / after the busway) and all of Flatbush Av. `pages/gfx.py` holds the shared SVG graphics and `pages/shell.py` the page CSS and wrapper. `build_a.py`, `build_b.py`, `build_s.py` are the three earlier pages that share those modules.
  - `ext/` : two outside inputs (`tsp_nodes.csv`, signal-priority locations and dates; `spdata.json`, plan corridor extents from an earlier report).
- `bap_corridors/` : corridor machinery used for the interactive delay curves (`lib.py`, `build.py`, `corridors.py`) and the cached Flatbush profile.
- `bus_delay_page/` : the methods page. `shell.py` reads its tooltip CSS and script from `bus_delay.html`.

## Rebuilding the page

From the repo root, with the project virtualenv:

    cd aaga/pitch-materials/nyt_research/pages
    ../../../../.venv/bin/python build_f.py        # writes pitch_f.html

This needs only the files in this folder plus `outputs/network/nyc/date_attrs.json`.

## Not included

- The large intermediate tables (`cube/`, `pz_*`, `ps_*`, `ref.pkl`, `aligned.npy`, about 3 GB). They are rebuilt from `outputs/network/nyc` by `cube.py`, then `run_city.py`, `rerun1.sh`, `rerun2.sh`.
- `ext/city_seg_day.parquet` (138 MB, used only by `events.py`) and the OpenStreetMap extracts used to build `X.parquet`.
- The Tremont profile is cached in `pages/f_tremont.pkl`; delete it to recompute from `traj_speed`.

## Caveats

- The split between stops, lights and other traffic depends on where the baseline is drawn; on streets with a light every block, other traffic is overstated.
- The 42nd Street split by cause for noon to 8pm is an estimate: delay curves are built per day, so each cause's whole-day delay is scaled by the located slow-moving time in those hours.
- Some scripts were run interactively during the research and may need small path fixes to run end to end.
