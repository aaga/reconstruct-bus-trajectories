# NYC bus research for an opinion pitch (23 Nov 2026)

Python: the repo's `.venv/bin/python` (run scripts from this folder).

Data products
- cube/day_YYYY-MM.parquet : seg_id x 10 ft bucket x service_date (n, sum_dt), pooled over routes, from traj_speed. cube/all.parquet: all days.
- cube/ev_hour.parquet, cube/trav_hour.parquet : located slow events and segment travel time by hour and weekday flag.
- ref.pkl / ref_zones.parquet : delay zones (stop / nearside / signal / other) for every segment, from the all-days curve (eng.py, period.py).
- pz_*.parquet / ps_*.parquet : zone seconds and segment totals per period (week_wd, daytype, tsp, win, day). Fixed zone bounds, each period's own floor.
- city_seg_all.parquet, city_zone_all.parquet, X.parquet (+OSM), X_bap.parquet (+Bus Action Plan flag), bundle.parquet (per signal approach), aligned.npy (signal-aligned curves).
- osm/ : seg_osm, node_osm, seg_feat (built by a sub-agent; README there).

Analyses: bundle.py (near/far-side, turns), tsp.py + tsp_an.py, ace.py (cameras), wc.py (42nd St match days), street.py (before/after by street), twins.py (matched corridors),
bap.py (plan corridors -> streets, street ranking), recs.py (recommendations, map geometry), figdata.py / figdata2.py (figure data).

Pages: pages/gfx.py (all SVG graphics), shell.py (CSS + page wrapper), build_a.py (pitch: streets the plan missed), build_b.py (pitch: what worked), build_s.py (summary).
