"""Shared configuration for the trajectory-clustering exploration.

Study corpus: TransLink route 99 B-Line (Broadway), westbound to UBC
(direction_id=1, shape 317528), from the public R2 realtime archive
(2026-06-23 .. 2026-06-29 UTC), reconstructed with LOCREG-PCHIP at
bandwidth 8.

TransLink's GTFS-RT vehicle positions carry static-GTFS trip_ids, so trips
are identified exactly (no CTA-style trip_id-reuse heuristics needed), and
``shapes.txt`` carries ``shape_dist_traveled`` in **kilometres** (unlike
CTA's feet) — the loaders here convert to metres directly rather than going
through ``dataio.gtfs``.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
for p in (str(REPO / "src"), str(REPO)):
    if p not in sys.path:
        sys.path.insert(0, p)

# ---------------------------------------------------------------- corpus
AGENCY = "translink"
ROUTE_ID = "6641"           # 99 B-Line (Broadway)
SHAPE_ID = "317528"         # WB: Commercial-Broadway -> UBC Exchange
DIRECTION_ID = "1"
TZ = "America/Vancouver"

GTFS_ZIP = REPO / "data" / "gtfs" / "translink_gtfs.zip"
PINGS_PARQUET = REPO / "caches" / "translink_r99_all.parquet"

# ---------------------------------------------------------------- outputs
OUT_DIR = REPO / "outputs" / "clustering"
TRAJ_JSON = OUT_DIR / "trajectories_r99_wb_bw8.json"
META_CSV = OUT_DIR / "trip_meta_r99_wb.csv"
PROFILE_NPZ = OUT_DIR / "profiles_r99_wb.npz"
FIG_DIR = OUT_DIR / "figures"

# ---------------------------------------------------------------- reconstruction
BANDWIDTH = 8               # LOCREG k-NN count (the user-specified bw=8)
DEGREE = 3
MAX_PERP_M = 80.0

# ---------------------------------------------------------------- trip QC gates
MIN_PINGS = 30              # ~7.5 min at TransLink's ~15 s cadence
ORIGIN_TOL_M = 500.0        # first ping within this of shape start
TERM_TOL_M = 300.0          # truncate at first ping within this of shape end
                            # (the 317528 shape overshoots the UBC Exchange bay
                            # where WB buses actually stop reporting by ~150-280 m)
GAP_MAX_S = 240.0           # max inter-ping gap while moving (see GAP_MOVE_M)
GAP_MOVE_M = 100.0          # gaps with less movement than this are holds, not
                            # data loss (TransLink vehicles go silent when parked)
MAX_TRIP_H = 2.5
MIN_SPAN_FRAC = 0.90        # truncated trip must cover >= 90% of shape length

# ---------------------------------------------------------------- profiles
# Common distance grid for the space-aligned representation t(d):
# clip the ends slightly so every kept trip covers the full grid.
GRID_D0_M = 200.0
GRID_STEP_M = 50.0
