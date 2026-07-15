"""Shared configuration for the ping-frequency sensitivity analysis.

The question under study: *what GPS ping frequency is necessary and sufficient
for a faithful reconstruction of bus trip trajectories?*

Benchmark = the dense VTRAK feed in ``data/highfreq-VTRAK`` (measured device
cadence: 2 s — see README.md in this folder). Seven successively-halved
datasets (4, 8, 16, 32, 64, 128, 256 s median cadence) are compared against it.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# Make src/ (core, dataio, ...) and the repo root (analysis.*) importable the
# same way every other pipeline script in this repo does it.
for p in (str(REPO / "src"), str(REPO)):
    if p not in sys.path:
        sys.path.insert(0, p)

# ---------------------------------------------------------------- inputs
HIGHFREQ_DIR = REPO / "data" / "highfreq-VTRAK"
DOOR_CSV = HIGHFREQ_DIR / "bus_state_hist_highfreq-VTRAK_06-11_to_06-17.csv"
GTFS = REPO / "data" / "gtfs" / "cta_gtfs.zip"

VEHICLES = ("1566", "8089", "8099")

# ---------------------------------------------------------------- outputs
OUT_DIR = REPO / "outputs" / "frequency_analysis"
CACHE_DIR = OUT_DIR / "cache"
RESULTS_DIR = OUT_DIR / "results"
FIG_DIR = OUT_DIR / "figures"

# ------------------------------------------------------- downsampling ladder
# Level k keeps every 2^k-th ping of the deduped benchmark stream. Level 0 is
# the benchmark itself. Median cadence at level k = BASE_CADENCE_S * 2^k.
BASE_CADENCE_S = 2.0
N_LEVELS = 8  # levels 0..7 -> 2, 4, 8, 16, 32, 64, 128, 256 s

def level_cadence_s(level: int) -> float:
    return BASE_CADENCE_S * (2 ** level)

# ------------------------------------------------------- reconstruction
# LOCREG bandwidth is a k-nearest-neighbour count, so a fixed k means a
# cadence-dependent time window. Fair comparison across cadences = hold the
# *time window* roughly constant and derive k per dataset.
#
# Window choice (validated against the AVL door ground truth on the 8 busiest
# trips): windows of 20-60 s all keep >=96% of door-open seconds below 5 mph
# at the benchmark cadence, while a 120 s window washes short dwells out
# (70%). W=40 s gives k=20 at 2 s — exactly Huang et al.'s bandwidth=20 — and
# k=5 (the repo's CTA BusTime convention) at cadences >= 16 s.
LOCREG_WINDOW_S = 40.0
BANDWIDTH_MIN = 5   # need >= degree+2 points for a stable cubic LOCREG fit
BANDWIDTH_MAX = 20  # benchmark cap (40 s @ 2 s cadence)

def bandwidth_for_cadence(cadence_s: float) -> int:
    return int(round(min(max(LOCREG_WINDOW_S / cadence_s, BANDWIDTH_MIN), BANDWIDTH_MAX)))

# ------------------------------------------------------- delay detection
DENSE_DT_S = 1.0          # evaluation grid for all metrics + event detection
SLOW_MPH = 5.0            # chapter §3.3 slow threshold
MIN_EVENT_S = 10.0        # min slowdown duration (comparison.py convention)
MPS_TO_MPH = 2.23694

# ------------------------------------------------------- trip QC
MIN_PINGS = 200           # >= ~7 min of 2 s pings
MAX_MEDIAN_CADENCE_S = 3.0
MAX_GAP_S = 60.0          # max time gap inside the benchmark ping stream
MIN_ON_ROUTE_FRAC = 0.70
MIN_FORWARD_M = 3000.0
WINDOW_PAD_S = 60.0
MAX_TRIP_H = 3.0

# Deadhead / non-revenue route ids to exclude when reading the door CSV.
NON_REVENUE = {"", "0", "992", "999", "None"}

def is_revenue_route(route_id: str) -> bool:
    r = str(route_id).strip()
    return r not in NON_REVENUE and not r.startswith(("PI", "PO", "DH"))

# ------------------------------------------------------- metric tolerances
POS_TOL_M = 50.0          # M1: |dx| tolerance (about half a Chicago block)
SPEED_TOL_MPH = 2.5       # M2: |dv| tolerance (half the slow threshold)
EVENT_MATCH_MIN_OVERLAP = 0.5  # M4: overlap >= 50% of the shorter event
MIN_DOOR_DWELL_S = 10.0   # M6/M7: only AVL door events long enough to detect
# AVL dwell_time occasionally spans a whole terminal layover (20+ min) during
# which the bus demonstrably moves — those rows are not "door open at a stop"
# ground truth. Real stop dwells are < ~3 min.
MAX_DOOR_DWELL_S = 180.0
