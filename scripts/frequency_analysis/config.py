"""Shared configuration for the ping-frequency sensitivity analysis (v2).

Corpus: the OneDrive highfreq-VTRAK feed (2s cadence) for CTA buses 1566,
8089, 8099, 2026-06-11 -> 2026-08-10. Trips are cut with the R2 BusTime
archive (``caches/realtime_archive``, ``agency=cta``); reconstruction methods
follow Robbennolt, Munira & Boyles (2025), arXiv:2509.00119
(``docs/UTAustin_trajectories.pdf``).

Baseline truth for all agreement metrics = ``BASELINE`` below (default:
VCHIP-ME run on the full 2 s feed). Change that one constant to re-score
against a different reference.
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
HIGHFREQ_DIR = Path(
    "/Users/ashwinagarwal/Library/CloudStorage/OneDrive-ChicagoTransitAuthority/highfreq-VTRAK"
)
DOOR_CSV = HIGHFREQ_DIR / (
    "bus_state_hist_highfreq_june_11_to_aug_11_exported_at_202608111336.csv"
)
R2_DIR = REPO / "caches" / "realtime_archive"
R2_AGENCY = "cta"          # full-fleet ~15 s BusTime archive (complete coverage)
GTFS = REPO / "data" / "gtfs" / "cta_gtfs.zip"

VEHICLES = ("1566", "8089", "8099")

# All wall-clock work happens in naive America/Chicago time — the native
# clock of VTRAK ``dtime`` and the AVL ``event_time``. The study window
# (Jun 11 – Aug 10) contains no DST transition, so naive arithmetic is safe.
LOCAL_TZ = "America/Chicago"

# ---------------------------------------------------------------- outputs
OUT_DIR = REPO / "outputs" / "frequency_analysis"
CACHE_DIR = OUT_DIR / "cache"
RESULTS_DIR = OUT_DIR / "results"
FIG_DIR = OUT_DIR / "figures"

# ------------------------------------------------------- downsampling ladder
# Target cadences in seconds. Feed at cadence c keeps every (c/2)-th row of
# the deduped per-vehicle 2 s stream (row index 0 always kept), per the spec:
# "literally taking every Nth row ... N = target frequency / 2".
FREQS_S = (2, 4, 6, 8, 10, 12, 14, 16, 24, 32, 48, 64, 96, 128)
BASE_CADENCE_S = 2

def stride_for(freq_s: int) -> int:
    assert freq_s % BASE_CADENCE_S == 0
    return freq_s // BASE_CADENCE_S

# ------------------------------------------------------- methods & baseline
# Currently under study: position-only PCHIP vs velocity-aware VCHIP-ME.
# The tunable methods (PCHIP-VCHIP, LOCREG-PCHIP, LOCREG-PCHIP-V) are parked
# for now (2026-08-11) — tuning drove all of them onto these two anyway; the
# implementations and tuning grids below remain ready to re-enable.
METHODS = ("PCHIP", "VCHIP-ME", "LSEG", "LOCREG-PCHIP", "LOCREG-PCHIP-V",
           "V-SPLINE-ME")

# Baseline truth: (method, cadence_s, params). Swap here to re-reference.
BASELINE = ("VCHIP-ME", 2, {})

# The real R2 BusTime feed (position-only, ~25 s median cadence) is scored as
# a reference point on every plot to validate the downsampling methodology.
R2_METHOD = "PCHIP"

# ------------------------------------------------------- tuning
# Train/test split (stratified by vehicle x route, fixed seed): the LARGER
# 2/3 goes to training so the per-frequency k grid means are stable; all
# models are reported on the smaller held-back test subset. 5-fold CV over
# the training trips checks that the argmin k is stable across folds.
TUNE_SEED = 20260811
TUNE_FRACTION = 1 / 3   # smaller train / bigger test (2026-08-14 stability check;
                        # the 2/3-train round is archived as *_train197.*)
N_FOLDS = 5

# Parameter bounds (rationale in README):
#  - alpha (PCHIP-VCHIP): the paper defines alpha in [0, 1]; we search the
#    full closed interval at 0.1 resolution.
#  - k (LOCREG-PCHIP): tricube-weighted CUBIC local fit needs >= 5 points
#    (degree+2) for a stable solve -> lower bound 5. Upper bound 30 points:
#    at the 2 s base cadence that is a ~60 s time window, the level beyond
#    which this repo's earlier door-truth validation showed short stop
#    dwells get smoothed away entirely. (At sparse cadences even k=5 spans a
#    long window — an inherent property of k-NN bandwidths.)
#  - kx, kv (LOCREG-PCHIP-V): same bounds, coarser joint grid to keep the
#    2-D search tractable.
ALPHA_GRID = tuple(round(a * 0.1, 1) for a in range(11))
# k grids extended upward (2026-08-14) so the optimum can't lock to the
# boundary now that M1/M2 score against noisy measured pings (smoothing may
# genuinely pay); lower bound 5 is still the cubic stability floor.
K_GRID = (5, 7, 9, 12, 16, 20, 25, 30, 40, 50)
KXV_GRID = (5, 9, 15, 25, 40)
# V-SPLINE-ME: gamma = velocity-observation weight (doppler trust), eta =
# adaptive curvature-penalty scale (eq 41). Decades chosen from a probe on
# tuning trips: the M2 optimum sits at extreme gamma (trust doppler), the
# M1 optimum at modest gamma / tiny eta.
VSPLINE_GAMMA_GRID = (1.0, 1e1, 1e2, 1e3, 1e4, 1e5, 1e6)
VSPLINE_ETA_GRID = (1e-3, 1e-2, 0.1, 1.0, 3.0, 10.0)

# ------------------------------------------------------- holdout (M1/M2)
# Paper-style data-driven test: hold out a random 5% of each trip's 2 s
# pings (one common set per trip, removed from EVERY feed's knots) and
# score reconstructions against the MEASURED position/speed at those points.
HOLDOUT_FRACTION = 0.05
HOLDOUT_SEED = 20260812

# ------------------------------------------------------- evaluation
DENSE_DT_S = 1.0            # common evaluation grid step
MPS_TO_MPH = 2.23694
MPH_TO_MPS = 1.0 / MPS_TO_MPH
MPS_TO_FTPS = 3.28084       # speed reported in ft/s to match the paper

# Metric 3: doors-open stopped, thresholds in ft/s (paper Table 3 uses
# 2 / 5 / 10 ft/s). Dot at the main threshold; whiskers at the others.
STOP_FTPS_MAIN = 5.0
STOP_FTPS_WHISKERS = (2.0, 10.0)
DOOR_EVENT_TYPES = {"3", "4", "5"}   # Serviced / UnServiced / Unknown stop
MIN_DOOR_DWELL_S = 10.0
MAX_DOOR_DWELL_S = 180.0    # longer spans are terminal layovers, not dwells

# Knee rule per plotted metric: the ring marks the LAST ladder interval that
# still meets the criterion. ("rel", f): value within f of the curve's own
# 2 s value (old analysis convention, for higher-is-better metrics).
# ("abs", v, "<="): value still below an absolute threshold (for error
# metrics, which have no natural 100% anchor). Thresholds: 10 m ~ GPS/stop
# discrimination scale; 5 mph = the slow threshold; 10% zone error.
KNEE_RULES = {
    "rmse_x_mean": ("abs", 10.0, "<="),               # m
    "rmse_v_mean": ("abs", 5.0 * MPH_TO_MPS, "<="),   # 5 mph, stored in m/s
    "door_stop_pct_5.0": ("rel", 0.90),
    "doorloc_med_m": ("abs", 10.0, "<="),             # m
    "zone_wmape_pct": ("abs", 10.0, "<="),            # %
    "slow_f1": ("rel", 0.90),
    "ev_f1": ("rel", 0.90),
    # M4a/M4b: no knee — realism check, monotone by construction
}

# ------------------------------------------------------- physical realism
# Paper Table 3 acceleration bounds (ft/s^2 -> m/s^2): M4a tight, M4b loose.
FT_TO_M = 0.3048
ACCEL_TIGHT = (-5.79 * FT_TO_M, 4.26 * FT_TO_M)
ACCEL_LOOSE = (-7.77 * FT_TO_M, 5.43 * FT_TO_M)

# ------------------------------------------------------- delay & signal zones
SLOW_MPH = 5.0            # M6/M7 slow threshold
MIN_EVENT_S = 10.0        # M7: min slowdown-event duration
EVENT_MATCH_MIN_OVERLAP = 0.5   # M7: overlap >= 50% of the shorter event
SIGNAL_ZONE_M = 300 * FT_TO_M   # M5: zone upstream of each signal
INTERSECTIONS = REPO / "caches" / "cta" / "intersections.json"

# ------------------------------------------------------- trip QC
# A trip window is the span of consecutive same-trip R2 rows. "Complete"
# means the VTRAK 2 s stream fully covers that window:
MAX_VTRAK_GAP_S = 10.0      # no internal gap; edges within this of the window
MIN_TRIP_S = 300.0
MAX_TRIP_S = 3.0 * 3600
MIN_PINGS = 150
MIN_ON_ROUTE_FRAC = 0.70
MIN_FORWARD_M = 3000.0
