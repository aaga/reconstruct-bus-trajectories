# Bus accelerometer analysis (record-a-ride observation trips)

Analyzes the ~59 Hz phone accelerometer/gyro captured by record-a-ride on the
9 dashboard observation trips, against the phone's own 1 Hz GPS.

Data: trips.html export folders under `~/Downloads/record-a-ride_trips_*/<key>/`
(`motion.csv`, `pings.csv`, `meta.json`). Outputs → `outputs/accel_analysis/`.

## The orientation problem — and why it dissolved

The plan was: find gravity per frame, project acceleration onto the plane
perpendicular to it. **The recorded data contains no gravity to find** — the
app records `devicemotion.acceleration` (OS sensor-fusion *linear*
acceleration, gravity already removed per frame; median |a| ≈ 0.1–0.7 m/s²
on all 11 trips checked, never ~9.8). That's better than the plan: the OS did
the per-frame recalibration on-device. What remains is expressed in the
rotating phone frame, so we still don't know "forward" — but:

- **|a| is rotation-invariant**, and
- a bus **cannot sustain vertical acceleration** for ≥1–2 s (flat Chicago,
  no sustained climbs), while bumps/vibration are broadband high-frequency,

so a zero-phase 0.4 Hz Butterworth low-pass (the "sustained ≥1–2 s" rule)
leaves |a_lowpassed| ≈ |a_xy|, the horizontal magnitude — no orientation
estimate needed. A fallback projection path exists for any trip that *does*
record gravity (auto-detected loudly per trip; none of the 9 needed it).

Phone-handling periods (rider manipulating the phone: sustained rotation
> 60°/s, padded ±1 s) are masked and shaded in figures — 0.6–4.1% of samples.

**Future recordings** (record-a-ride updated 2026-07-15) additionally carry
`agx, agy, agz` = accelerationIncludingGravity. Its low-passed direction is
the per-frame gravity vector — the vertical reference that unlocks *signed*
lateral acceleration (l̂ = ĝ × f̂) instead of the magnitude-only a_lat
below. Old recordings have those columns blank.

## Files

| file | role |
|---|---|
| `common.py` | export discovery, motion loading + gravity-mode detection, per-segment zero-phase filtering, handling mask, GPS speed/accel (LOCREG-PCHIP on cumulative path length — no map needed) |
| `magnitude_analysis.py` | per-trip figure: sustained \|a_xy\| vs GPS-inferred \|f″\| + speed panel; agreement stats → `results/magnitude_stats.csv` |
| `steering_exploration.py` | pedal vs steering episode classification (below) → `results/steering_episodes.csv` + per-trip timeline figures |
| `forward_axis_fusion.py` | opportunistic forward-axis fusion: signed a_long / a_lat from accel+gyro with GPS-anchored pedal events → `results/fusion_stats.csv`, `fusion_drift_bins.csv`, `figures/fusion_*.png` |

Run from this folder: `uv run python magnitude_analysis.py` / `steering_exploration.py`
(optional `--keys`, `--extra-dir`).

## Magnitude results (9 dashboard trips)

Phone |a_xy| and GPS-inferred |a| agree well visually — every burst
co-occurs with a speed ramp, quiet floors during dwells, matching p95 scale
(phone 0.9–1.5 vs GPS 1.2–1.9 m/s²). Sample-wise Pearson r = 0.11–0.56
(median ≈ 0.39), limited mainly by bandwidth/lag mismatch: the GPS accel is
f″ of a ~15 s LOCREG window while the phone signal has ~2.5 s resolution, so
short pedal events appear in the phone trace at full amplitude but smeared
and attenuated in the GPS curve. The phone accelerometer is effectively a
*higher-resolution* accel sensor than anything derivable from 1 Hz GPS —
which is the point of collecting it.

## Steering vs pedal (exploratory)

**Negative result first** (kept in the code for the record): the literature
separates steering from pedal events with a reoriented gyro — V-Sense-style
bump-shaped yaw signatures (turn ≈ ±90° net heading, lane change ≈ 0°,
Chen et al. MobiSys'15) after Nericell-style virtual reorientation. Two
recovery routes were tried: PCA of rotation-rate vectors for the vertical
axis + integrated-yaw-vs-GPS-bearing validation, and direct least-squares
regression of the 3 gyro channels onto the GPS bearing rate (fit only on
clear turns, scanned over ±3 s lag). Both fail on essentially every window
(r ≈ 0.15; median |rotation| is ~5–7°/s whether or not the bus is turning).
**A rider's phone is rotationally decoupled from the bus** — body sway
swamps vehicle yaw. The mounted-phone assumption doesn't transfer to transit.

**What works instead:** vehicle yaw from the GPS track itself —
ω = d(bearing)/dt of the smoothed path (drift-free, orientation-free, valid
while v > 3 m/s, which is when steering accel exists). The kinematic
identity a_lat = v·ω predicts the steering (centripetal) share of the
measured |a_xy| with no accelerometer axes at all. Sustained episodes
(≥1.5 s over 0.35 m/s²) are then classified by the steering-explained
fraction:

| kind | rule |
|---|---|
| turn / steering (curve/lane) | a_lat/\|a_xy\| ≥ 0.6, split by net heading change ≥ 45° |
| accelerator / brake | a_lat/\|a_xy\| ≤ 0.35, sign of GPS dv/dt |
| mixed | in between (braking into / accelerating out of a turn) |
| low-speed | below the 3 m/s bearing floor (stop pull-in/out) |

Across the 9 trips: 783 episodes — brake 254, accelerator 176, low-speed
179, turn 53, curve/lane 50, mixed 71. Sanity checks in the figures: brake
bands sit on speed-curve descents, accelerator bands on ascents, turn bands
coincide with yaw spikes and |a_lat| tracking |a_xy|.

**Caveats:** |a_xy| is unsigned, so pedal direction comes from GPS dv/dt,
not the accelerometer; lane changes at low lateral accel fall below the
episode floor and are largely invisible; the a_lat prediction inherits GPS
bearing noise (5 s smoothing), so short jinks blur; "low-speed" episodes
(pulling in/out of stops) genuinely mix pedal and steering and are left
unsplit rather than guessed.

## Forward-axis fusion (`forward_axis_fusion.py`)

Opportunistic calibration: clean pedal events (sustained |a| ≥ 0.5 m/s²,
GPS-confirmed dv/dt, steering-quiet) measure the forward axis f̂ in the
phone frame (sign from GPS dv/dt); between anchors f̂ is propagated against
the phone's own rotation (gyro low-passed at 0.4 Hz so only sustained
rotation propagates, pure gyro — GPS yaw deliberately not folded in), forward
and backward from each anchor, blended. Then a_long = A_sus·f̂ (signed) and
a_lat = √(|A_sus|² − a_long²). The devicemotion gyro columns are
(alpha,beta,gamma) = rotation about (z,x,y) — reordered before integrating;
the ω sign convention was resolved empirically (−1, checked per trip).

Validation held-out against GPS (anchor windows excluded from scoring):

- **Signed longitudinal accel is recoverable**: r(a_long, f″_GPS) = 0.10–0.82
  across the 9 trips (median ≈ 0.63), sign agreement 66–78% (chance = 50%).
  Anchors are dense (median gap 13–32 s, 20–73 per trip).
- **Drift horizon ≈ 5–10 s**: pooled sign agreement is 78% within 5 s of an
  anchor, ~66% at 5–10 s, roughly flat (~65–75%) beyond — the axis blurs
  between anchors but is repeatedly rescued, exactly the re-anchoring bet.
- **The lateral channel does not validate well** (r_lat −0.2…0.5): a_lat is
  the residual magnitude, so any f̂ error leaks longitudinal energy into it,
  and the GPS lateral truth is small most of the time. Use the GPS-curvature
  method for steering; use the fusion for signed pedal detail.
- Trip quality varies with the rider: the weakest trip (r_long = 0.10)
  presumably had the phone loosely held/in motion; the best (0.82) tracks
  the GPS accel curve visibly peak-for-peak at 59 Hz resolution.
- **Turn check** (`figures/turns_*.png` — one zoomed panel per GPS turn
  event, plus `turn_contrast` in `fusion_stats.csv`): mean a_lat inside GPS
  turn events is 1.3–4.8× the outside-turn mean on 8 of 9 trips (the
  exception has only 2 detected turns). Long, real corners (≥4–5 s) show
  a_lat rising with |v·ω| inside the yellow span; many 2 s threshold
  crossings are GPS bearing noise where a_lat rightly stays quiet. So the
  weak *sample-wise* r_lat coexists with a real *event-level* turn signal.
