# Ping-frequency sensitivity analysis (v2)

*(The June-corpus v1 analysis is archived in `scripts/frequency_analysis_old/`
with its outputs in `outputs/frequency_analysis_old/`.)*

**Question.** How does GPS ping frequency interact with the choice of
trajectory-reconstruction algorithm — position-only vs. velocity-aware, with
and without LOCREG smoothing?

**Corpus.** The OneDrive highfreq-VTRAK feed
(`~/Library/CloudStorage/OneDrive-ChicagoTransitAuthority/highfreq-VTRAK`):
CTA buses **1566, 8089, 8099**, 2026-06-11 → 2026-08-10, deduped device
cadence 2 s (`dtime` is the device clock; the parquet `timestamp` is only the
scraper poll time). 294 complete revenue trips.

**Methods.** Reconstruction algorithms from Robbennolt, Munira & Boyles
(2025), arXiv:2509.00119 (`docs/UTAustin_trajectories.pdf`), implemented from
the paper in `methods.py`:

| method | position/velocity | smoothing | tunables | status |
|---|---|---|---|---|
| LSEG | position-only | no | — | active (added 2026-08-13) |
| PCHIP | position-only | no | — | active |
| VCHIP-ME | velocity-aware | no | — | active |
| LOCREG-PCHIP | position-only | LOCREG | `k` | active (re-enabled 2026-08-14) |
| LOCREG-PCHIP-V | velocity-aware | LOCREG | `kx`, `kv` | active (re-enabled 2026-08-14) |
| V-SPLINE-ME | velocity-aware | joint soft-position fit | `gamma`, `eta` | active (added 2026-08-14) |
| PCHIP-VCHIP | blend | no | `alpha` | parked (see v1 findings) |

## Design decisions

- **Downsampling ladder**: target cadences 2 (original), 4, 6, 8, 10, 12,
  14, 16, 24, 32, 48, 64, 96, 128 s. The feed at cadence `c` keeps every
  `(c/2)`-th row of the vehicle's continuous 2 s stream (row 0 kept), then is
  trimmed to trip windows — literally "every Nth row, N = frequency / 2".
- **Trip windows from the R2 archive** (`caches/realtime_archive`,
  `agency=cta`, ~15 s full-fleet BusTime feed): a window is the span of
  consecutive same-`(trip_id, route_id, start_date)` rows for the vehicle.
  **Complete trip** = the VTRAK 2 s stream covers the whole window — first/
  last ping within 10 s of the window edges, no internal gap > 10 s
  ("R2 span + VTRAK only" rule, chosen 2026-08-11).
- **1-D position** = distance along the trip's GTFS shape
  (`analysis.comparison.choose_shape` + map matcher), matched **once** at
  2 s; downsampled feeds are row-subsets so every feed sees identical x
  values. A paper-style outlier pass precedes reconstruction: drop points
  implying >45 mph forward vs the running front (the speed form of the
  paper's 500 ft rule, cadence-independent) or >200 ft backtracks; clamp
  the rest monotone.
- **Baseline truth** = `config.BASELINE` — VCHIP-ME on the full 2 s feed.
  One constant to change to re-reference every metric.
- **Common grid**: every reconstruction is evaluated on the trip's 1 s grid
  spanning the intersection of all 14 feeds' knot ranges, so every frequency
  is scored on identical seconds.
- **Timezones**: everything is naive America/Chicago (native clock of VTRAK
  `dtime` and AVL `event_time`; R2 UTC timestamps are converted). Jun–Aug
  has no DST transition.

## Tuning (v2.2, 2026-08-14)

Train/test split **stratified by vehicle × route** (seed in `config.py`),
currently **1/3 train / 2/3 test (97/197)**; every model is reported on the
test subset. Tuning target = the M1/M2 **holdout** scores (MAE vs measured
pings), per *(method × frequency × metric)*; 5-fold CV over the training
trips checks that the argmin is fold-stable
(`results/tuning_cv_report.csv`).

**Stability checks (2026-08-14):** flipping the split (197-train → 97-train,
disjoint trips) reproduces the tuned M2 bandwidths *exactly* at every
frequency, and the parameterless methods' M1/M2 curves agree within a few
percent across full-294 / 97-test / 197-test subsets (worst ~5% at 128 s) —
neither the k choices nor the curves are sampling artifacts. The 2/3-train
round is archived as `results/*_train197.*`.

- `k` ∈ {5, 7, 9, 12, 16, 20, 25, 30, 40, 50} — lower bound 5 is the
  tricube local **cubic** stability floor (≥ degree+2 points); the top was
  extended (2026-08-14) so the optimum cannot lock to the boundary.
- `kx, kv` ∈ {5, 9, 15, 25, 40}² — coarser joint grid for tractability.
- `alpha` ∈ {0.0, 0.1, …, 1.0} (PCHIP-VCHIP, currently parked).

M1 (position) and M2 (speed) each get their own tuned variant; the other
metrics reuse the M2 (speed-tuned) parameters.

## Metrics (all: x-axis = ping interval, one line per method)

1. **M1 positional agreement** — paper-style **5% holdout validation**
   (since 2026-08-12): one random 5% of each trip's 2 s pings (seeded,
   common across frequencies) is removed from every feed's knots; each
   reconstruction is scored against the *measured* position at those
   held-out points. Plotted: mean per-trip MAE (m); RMSE in `summary.csv`.
   The earlier baseline-referenced grid RMSE/MAE columns remain in the CSVs.
2. **M2 speed agreement** — same holdout scoring against the *measured*
   speed at held-out pings, in ft/s to compare with the paper. Note the
   truth carries the sensor's integer-mph quantization (~±0.7 ft/s).
3. **M3 doors-open stopped %** — share of AVL door-open seconds
   (`bus_state_hist_highfreq_june_11_to_aug_11…csv`: event types 3/4/5,
   `event_time` → `event_time + dwell_time`, dwell 10–180 s; longer spans
   are terminal layovers) where the reconstructed speed is below 5 mph.
   Whiskers per point span the 1 / 3 / 7.5 / 10 mph thresholds; whiskers are
   dodged horizontally, dots sit at the true frequency.
4. **M3b bus-stop location error** — per door event, the reconstruction's
   position at mid-dwell (`event_time + dwell/2`) minus the AVL-reported
   door lat/lon projected onto the same route shape (1-D distance, m);
   plotted as the median across per-trip means (the pooled mean is dominated
   by rare km-scale mis-snapped AVL points on looping shapes). AVL rows with
   lat/lon 0.0 (no fix) are excluded.
5. **M4a/M4b reasonable acceleration** — % of grid seconds whose finite-
   difference acceleration lies within the paper's tight
   (−5.79…4.26 ft/s²) / loose (−7.77…5.43 ft/s²) bounds. Not
   baseline-referenced — a physical-realism check on each reconstruction.
6. **M5 signal-zone travel time** — travel time through the 300 ft
   immediately upstream of each signalized intersection (registry:
   `caches/cta/intersections.json`, signalized ControlPoints anchored to the
   same shape ruler), reconstructed crossing times vs. baseline; plotted as
   weighted MAPE (Σ|Δtt| / Σtt_base).
7. **M6 delay time** — per-second F1 of the v < 5 mph slow mask vs. the
   baseline's, micro-pooled over trips (old frequency-analysis M3).
8. **M7 distinct delay events** — event-level F1: slowdown events =
   contiguous slow runs ≥ 10 s; greedy 1:1 matching by temporal overlap
   ≥ 50% of the shorter event (old frequency-analysis M4).

**R2 reference mark.** Every plot carries an **X** at the real R2 BusTime
feed's pooled median cadence (~25 s): the actual archive pings for each trip
(position-only — CTA's R2 speed field is empty — so scored with PCHIP),
map-matched to the same shapes. If the X lands on the ladder curves, thinned
VTRAK is a faithful stand-in for a genuinely sparse feed; where it lands
above them, the real feed carries pathologies (stale positions, gaps) that
idealized downsampling cannot reproduce.

**Knees.** A ring marks the last ladder point that still meets the metric's
rule (`config.KNEE_RULES`): for higher-is-better metrics (M3/M6/M7), ≥ 90%
of the curve's own 2 s value (the old analysis convention); for error
metrics, an absolute threshold (M1/M3b: 10 m; M2: 5 mph — the slow
threshold; M5: 10%), since error curves start at ~0 and have no natural
100% anchor. The ring is placed before the *first* failure, so brief
recoveries after a dip don't count. M4a/M4b get no knee: they *improve*
with sparsity (smoother reconstructions are trivially "reasonable"), so a
knee would mislead.

## Files

| file | role |
|---|---|
| `config.py` | paths, ladder, method list, baseline, tuning grids, QC bounds |
| `trip_index.py` | R2 windows → complete-VTRAK filter → shape match → cache |
| `methods.py` | the five reconstruction algorithms (paper Algorithms 1–4) |
| `pipeline.py` | feeds by stream-index stride, common grid, scoring engine |
| `r2_feed.py` | projects the real R2 pings onto each trip (reference feed) |
| `run_analysis.py` | `--stage split / tune / eval / summarize` orchestration |
| `plot_results.py` | the nine metric figures |

Run (from `scripts/frequency_analysis/`):

```bash
uv run python trip_index.py                  # (re)build the trip cache
uv run python r2_feed.py                     # project real R2 pings onto trips
uv run python run_analysis.py --stage eval   # scores all trips + R2 reference
uv run python plot_results.py
# --stage split / tune only apply when tunable methods are active;
# --stage summarize re-pools summary.csv from per_trip_results.csv
```

Outputs: `outputs/frequency_analysis/{cache,results,figures}`.

## Findings

### v2.5 addendum (2026-08-20): fleet-wide March 2026 AVL evaluation

`fleet_march.py`: every CTA bus, 2026-03-01..31, trips segmented by the
archive's own (trip_id, pattern_id) runs, shapes via the 5-digit
pattern-suffix convention against the repo GTFS snapshot (patterns absent
from the snapshot are excluded), QC as in the main analysis plus the
120 s coverage rule. **421,149 trips / 1,881 buses / 123 routes** scored
with the same 5% self-holdout M1/M2 and door-parquet M3
(`Bus State History/bus_state_hist`, event types 3/5).

- **Fleet ping frequency: median 15.0 s, IQR 13-19 s** (3-vehicle covered
  subset: 14.5 s) — the ~15 s event-driven cadence is fleet-wide, not a
  study-vehicle quirk.
- Pooled fleet metrics sit within ~1 m / 0.4 ft/s / 3 pts of the 3-vehicle
  marks: PCHIP 19.9 m / 5.4 ft/s / 86.9%; VCHIP-ME 15.8 m / 4.8 ft/s /
  87.6% (hollow marks on the M1/M2/M3 figures). The study vehicles are
  representative.
- **LOCREG is fully degenerate at this cadence** (2,125-trip subsample,
  `fleet_march_locreg.csv`): k=5 / (5,5) is optimal for BOTH variants on
  ALL THREE metrics, with steep penalties beyond (M3 collapses 87% -> 24%
  by k=16 — a ~4 min window erases dwells). Confirms the ladder tuning:
  smoothing only pays at 2-6 s cadences; at ~15 s it is useless.


### v2.4 addendum (2026-08-20): real low-frequency AVL archive as reference

**Units (verified 2026-08-20, after a wrong-unit round):** speed units are
NOT shared across feeds. Distance/time regression per feed: VTRAK = mph
(fitted 0.4472 m/s-per-unit vs 0.44704) and the AVL archive = **ft/s**
(0.2988 vs 0.3048). With both converted to m/s, nearest-in-time AVL vs
VTRAK speeds agree to 0.90 ft/s MAE (corr 0.995) — the two independent
sensors cross-validate. M1/M2 for the AVL marks use the paper-literal 5%
self-holdout on the AVL feed's own pings (the VTRAK-referenced variant is
kept as `vt_*` columns in `results/avl_reference.csv`).


The full-fleet CTA AVL archive (`~/Library/CloudStorage/OneDrive-…/CTA AVL
Archive/avl_archive`, daily parquets, naive Chicago clock, event-driven
~15 s median cadence, integer-mph speeds like VTRAK) was projected onto
every cached trip (`avl_feed.py`, 292/294 trips) and scored with PCHIP and
VCHIP-ME (`avl_eval.py` -> `results/avl_reference.csv`; X marks on
M1/M2/M3/M5 at the median cadence).

- **Coverage first**: 139/293 trips have an AVL hole > 2 min — almost all
  vehicle 8099, whose public-facing feed drops out for 10-20 min stretches
  (median per-trip worst gap 606 s vs ~31 s for 1566/8089). During one
  walk-through void the R2 feed was dark too while VTRAK + door events
  continued (a serviced stop mid-void), so these are public-feed tracking
  dropouts, not vehicle downtime. Reconstruction across such a hole
  produces km-scale errors, so the plotted marks use only the 154
  **covered** trips (max gap <= 120 s); the holes are reported as a
  coverage statistic instead.
- **Covered trips** (median cadence 14.5 s): M1 15.4 m (PCHIP) / 14.4 m
  (VCHIP-ME) vs 8.6/5.7 m for the idealized 16 s ladder — the real feed
  costs ~1.8-2.5x its nominal cadence. M2 3.7/4.1 ft/s (vs 3.6/2.5);
  M3 doors-open ~90% (vs 92-95%); M5 zone error 12-14% (vs 4-6%) —
  operationally the covered AVL behaves like a ~24-32 s ideal feed.
- With real coverage, the speed channel helps position again (VCHIP-ME
  beats PCHIP on M1, 14.4 vs 15.4 m) but still trails the ladder's
  velocity-aware curves — archive speeds/positions are event-driven and
  not sampled at the same instants, so the channel-consistency premium of
  the dense analysis only partially survives.

### v2.3 addendum (2026-08-14): V-SPLINE-ME

V-SPLINE-ME (paper 2.2.7-2.2.9) was implemented with a banded O(n) solver
(the system is block-tridiagonal; ~2 ms per trip vs the paper's seconds)
and tuned like the LOCREGs (gamma = doppler-trust weight, eta = adaptive
curvature scale; grids in `config.py`). On the 197-trip test set it is the
**overall winner**: best M2 at every cadence — **0.98 ft/s at 2 s**, close
to the 0.73 ft/s speed-only (VLIN) floor — while matching VCHIP-ME on M1
within ~0.1 m everywhere (and beating it at 96-128 s). Tuned params tell
the story: M2 wants gamma = 1e6 with heavy smoothing at 2-6 s (trust the
doppler, treat positions as soft anchors), M1 wants gamma ~ 1 with tiny
eta (near-interpolation). This is the soft-position architecture the M2
noise analysis called for: the only method able to exploit the accurate
speed channel instead of being constrained by it.


### v2.2 round of 2026-08-14 — holdout M1/M2, LOCREG revisited

M1/M2 were switched to **5% holdout validation** (truth = measured pings,
not the baseline reconstruction), LSEG was added, and the LOCREG pair was
un-parked and re-tuned per frequency on the new metrics (train/test =
197/97 trips stratified by vehicle x route; 5-fold CV on train confirms
fold-stable argmins, median 100% agreement; all models reported on the
97-trip test subset).

- **The M2 "dip" is a position-noise artifact, confirmed by experiment**
  (scratchpad: synthetic trips with controlled noise + real-data ablations).
  With sigma_x = 0 the dip vanishes and VCHIP-ME wins everywhere; the dip
  appears and scales linearly with injected sigma_x; a speed-only linear
  interpolator (VLIN) scores 0.73 ft/s at 2 s — 3.5x better than any
  position-interpolating method — proving the doppler speed channel is
  accurate and the noise enters through position. Mechanism: interpolating
  splines force the interior velocity to integrate exactly into the next
  noisy position (mid-gap speed ~ 1.5*(dx/h)-0.25(v_i+v_i+1)), so position
  noise passes into speed scaled by 1/h — sparser knots clean the estimate,
  denser ones amplify it. M1 has no such 1/h term, hence no dip.
- **Under the corrected metric, smoothing wins at dense cadence.** Tuned
  LOCREG-PCHIP picks k=12 at 2 s / k=7 at 4-6 s (M2) and drops to the
  k=5 floor from 8 s on; LOCREG-PCHIP-V similarly (kx15/kv25 at 2 s). Test-
  subset speed MAE at 2 s: **LOCREG-PCHIP 1.56 ft/s** vs LSEG 2.31, PCHIP
  3.08, VCHIP-ME 3.27 — and the smoothed curves are monotone (no dip).
  For **position (M1), k=5 everywhere still**: position noise (~1.5 m) is
  too small for smoothing to beat the interpolation bias it introduces.
  So the v1 conclusion "smoothing is useless" was an artifact of scoring
  against a noise-honoring baseline; against measured truth, smoothing is
  the right call exactly where the paper predicted — noisy data, dense
  cadence, derivative-sensitive products.


### v2 run of 2026-08-12 (294 complete trips, all scored, 9 metrics + R2 reference)

PCHIP / VCHIP-ME. M1 position RMSE m; M2 speed RMSE mph; M3 door-stop %
@5 mph; M3b stop-location error m (median of per-trip means); M5 zone wMAPE
%; M6 slow-state F1 %; M7 event F1 %:

| interval | M1 P/V | M2 P/V | M3 P/V | M3b P/V | M5 P/V | M6 P/V | M7 P/V |
|---|---|---|---|---|---|---|---|
| 4 s | 5.2/5.1 | 4.5/4.4 | 97.8/97.8 | 1.0/1.0 | 1.1/1.0 | 95.9/96.2 | 97.9/98.3 |
| 8 s | 7.9/8.3 | 5.0/4.9 | 97.5/97.6 | 1.0/1.0 | 2.2/2.0 | 93.4/94.1 | 97.0/97.3 |
| 16 s | 13.4/11.6 | 5.9/5.3 | 95.2/97.0 | 1.4/1.0 | 6.0/4.1 | 88.3/91.6 | 88.3/93.9 |
| 32 s | 29.2/20.4 | 7.6/6.3 | 78.3/90.9 | 11.2/4.5 | 20.2/11.5 | 76.4/84.2 | 62.7/77.6 |
| 64 s | 54.7/45.4 | 9.1/8.2 | 50.5/69.1 | 31.7/21.0 | 41.0/28.9 | 61.8/72.0 | 32.8/59.3 |
| 128 s | 89.6/90.7 | 10.0/9.7 | 24.9/49.7 | 55.5/50.2 | 54.5/46.6 | 48.4/60.8 | 13.8/40.5 |
| **R2 ~25.5 s** | 144.6 (med 35) | 8.1 | 69.0 | 16.9 | 32.8 | 73.5 | 65.2 |

Knees (ring on the plots): M1 ≤10 m through **10 s** (both); M2 ≤5 mph
through **8 s**; M3 ≥90% through 16 s (PCHIP) / **32 s** (VCHIP-ME); M3b
≤10 m through 24 s / **32 s**; M5 ≤10% through 16 s / **24 s**; M6 ≥90%
through 14 s / **16 s**; M7 ≥90% through 16 s / **24 s**.

- **R2 validation is two-sided.** Speed-referenced metrics put the real
  ~25 s feed roughly on the ladder (M6 73.5% vs PCHIP ~80% at 25 s; M7 65%
  ≈ PCHIP at 32 s) — thinning VTRAK is a fair model of *sparsity*. But
  position-anchored metrics sit far above the curves: position RMSE mean
  145 m (vs ~25 m on the ladder), stop location 17 m (vs ~6 m). The gap is
  heavy-tailed — the median trip scores 35 m, on the curve, while ~24% of
  trips exceed 200 m — and traces to the R2 feed's stale positions (the
  BusTime vehicle timestamp advances while lat/lon repeats) plus archive
  gaps. Conclusion: the ladder models bandwidth loss faithfully; it does
  NOT model the real feed's staleness pathology, which by itself costs more
  than the sparsity at today's ~25 s cadence.
- **Velocity-awareness still buys ~one octave** of cadence on M3/M5/M6/M7
  (VCHIP-ME at 32 s ≈ PCHIP at 16-24 s), and its knees sit one to two
  ladder steps later on every operational metric.
- **M4a/M4b** climb monotonically from ~77-80% (2 s) to ~100% (≥32 s) —
  realism improves as fidelity degrades, hence no knee.

### v1 run of 2026-08-11 (5 methods, tuned) - why the tunables are parked

The first round ran all five paper methods with per-frequency tuning
(alpha; k; kx,kv) on the stratified tuning half. Outcomes that motivated
parking them:

- Tuning drove every LOCREG bandwidth to the grid minimum k=5 (~no
  smoothing) at all 14 frequencies, monotone in k - against a dense,
  map-matched baseline the rapid oscillations are signal, matching the
  paper's dense-data finding. Note k=5 is the cubic stability floor, so
  this is a boundary conclusion ("less smoothing is always better"), not an
  interior optimum.
- PCHIP-VCHIP's alpha tuned to 1.0 (= VCHIP-ME) through 48 s, drifting to
  0.5-0.6 only at 96-128 s where it beat both parents on position
  (76 vs 82-84 m RMSE at 128 s) - worth revisiting if very sparse cadences
  become the focus.
- Full tuned-run artifacts remain in `results/` (`tuning_scores.csv`,
  `tuned_params.json`) from that run.
