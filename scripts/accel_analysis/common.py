"""Shared loading + signal processing for the bus accelerometer analysis.

Data source: record-a-ride trip exports (the trips.html "download" layout):
    <export_dir>/<trip_key>/{meta.json, pings.csv, events.csv, motion.csv}

Motion channels (see record-a-ride/sensors.js): ax/ay/az from devicemotion —
**linear acceleration in the rotating phone frame, gravity already removed by
the OS sensor fusion** on modern devices, with a documented fallback to
accelerationIncludingGravity on devices that lack it — plus gx/gy/gz rotation
rate (deg/s), at ~60 Hz.

Orientation strategy (per the analysis plan):
 - Gravity-removed trips (the normal case): the OS already re-references
   gravity every frame. |a| is rotation-invariant, and a bus cannot sustain
   vertical acceleration for >=1-2 s, so the low-passed 3-axis magnitude IS
   the horizontal (x-y) sustained-acceleration magnitude.
 - Gravity-included trips (fallback devices, auto-detected by median |a|):
   estimate gravity per frame with the same low-pass, project acceleration
   onto the plane perpendicular to it, then take the in-plane magnitude.
Every trip's mode is decided loudly at load time.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.signal import butter, filtfilt

REPO = Path(__file__).resolve().parents[2]
for p in (str(REPO / "src"),):
    if p not in sys.path:
        sys.path.insert(0, p)

from core.smooth import locreg_pchip  # noqa: E402

OUT_DIR = REPO / "outputs" / "accel_analysis"
FIG_DIR = OUT_DIR / "figures"
RESULTS_DIR = OUT_DIR / "results"

# The 9 dashboard trips (outputs/obs_trips/index.json).
DASHBOARD_KEYS = [
    "1781530040645_88362248", "1781531199995_88392853", "1781532879713_49499018",
    "1781534420859_1391", "1781556002569_379", "1781557500154_4949217",
    "1781558623220_88392853", "1781560962181_88365570", "1781661651457_88365606",
]

GRAVITY = 9.80665
# median |a| above this => the trip recorded accelerationIncludingGravity
GRAVITY_DETECT_MS2 = 5.0

# "sustained >= 1-2 s" -> keep content slower than ~0.4 Hz
SUSTAINED_CUTOFF_HZ = 0.4
FILTER_ORDER = 4
GAP_SPLIT_S = 0.5          # motion-stream gaps larger than this split segments

# phone-handling mask: sustained rotation faster than any bus maneuver
HANDLING_GYRO_DPS = 60.0   # |rotation rate| low-passed at 1 Hz
HANDLING_LP_HZ = 1.0
HANDLING_PAD_S = 1.0

GPS_MAX_ACC_M = 50.0       # drop pings with worse reported accuracy
GPS_LOCREG_BW = 15         # ~15 s LOCREG window on the ~1 Hz phone GPS
MPS_TO_MPH = 2.23694


# ------------------------------------------------------------------ loading

def find_export_dirs(roots=(Path.home() / "Downloads",)) -> list[Path]:
    """record-a-ride export folders, newest first."""
    out = []
    for root in roots:
        out += [p for p in root.glob("record-a-ride_trips_*") if p.is_dir()]
    return sorted(out, reverse=True)


def locate_trip(key: str, extra_dirs: list[Path] = ()) -> Path | None:
    """Find <key>/motion.csv in any export dir (newest export wins)."""
    for d in [*extra_dirs, *find_export_dirs()]:
        p = Path(d) / key
        if (p / "motion.csv").exists():
            return p
    return None


@dataclass
class MotionTrip:
    key: str
    path: Path
    meta: dict
    gravity_mode: str            # "removed" (OS linear accel) | "included"
    t: np.ndarray                # motion time, seconds since trip t0 (naive local)
    a_horiz: np.ndarray          # sustained horizontal accel magnitude (m/s^2)
    A_sus: np.ndarray            # sustained linear accel VECTOR (N,3), phone frame
    a_raw_mag: np.ndarray        # unfiltered |a| (diagnostics)
    G_raw: np.ndarray            # raw rotation rate (N,3) deg/s, devicemotion
                                 # column order (alpha,beta,gamma)=(about z, x, y)!
    G_lp: np.ndarray             # low-passed per-axis rotation rate (N,3) deg/s
    gyro_mag: np.ndarray         # low-passed |rotation rate| (deg/s)
    handling: np.ndarray         # bool mask: phone probably being handled
    hz: float
    segments: list[tuple[int, int]]   # contiguous index runs (gap-split)
    t0: pd.Timestamp             # wall clock of first motion sample
    gps: pd.DataFrame = field(default=None, repr=False)  # t, speed_mps, v_recon, a_recon


# ---------------------------------------------------------------- filtering

def _segments(t: np.ndarray, gap_s: float = GAP_SPLIT_S) -> list[tuple[int, int]]:
    """Contiguous [i0, i1) runs split where the sample clock jumps."""
    gaps = np.where(np.diff(t) > gap_s)[0]
    starts = np.r_[0, gaps + 1]
    ends = np.r_[gaps + 1, len(t)]
    return [(int(s), int(e)) for s, e in zip(starts, ends) if e - s > 10]


def lowpass(x: np.ndarray, hz: float, cutoff: float, order: int = FILTER_ORDER) -> np.ndarray:
    """Zero-phase Butterworth low-pass (padlen-safe)."""
    b, a = butter(order, cutoff / (hz / 2), btype="low")
    padlen = min(3 * max(len(b), len(a)), len(x) - 1)
    return filtfilt(b, a, x, padlen=padlen)


def _per_segment(t, X, hz, cutoff):
    """Low-pass each column of X per contiguous segment; NaN elsewhere-safe."""
    out = np.full_like(X, np.nan, dtype=float)
    for i0, i1 in _segments(t):
        for c in range(X.shape[1]):
            out[i0:i1, c] = lowpass(X[i0:i1, c], hz, cutoff)
    return out


# ---------------------------------------------------------------- pipeline

def load_motion(key: str, extra_dirs: list[Path] = ()) -> MotionTrip:
    path = locate_trip(key, extra_dirs)
    if path is None:
        raise FileNotFoundError(
            f"trip {key}: no export folder with motion.csv found — download it "
            f"via trips.html into ~/Downloads")
    meta = json.loads((path / "meta.json").read_text()) if (path / "meta.json").exists() else {}

    df = pd.read_csv(path / "motion.csv")
    ts = pd.to_datetime(df["timestamp"])
    t0 = ts.iloc[0]
    t = (ts - t0).dt.total_seconds().to_numpy()
    # exports are append-only chunks; enforce monotone time & dedupe
    order = np.argsort(t, kind="stable")
    t = t[order]
    df = df.iloc[order].reset_index(drop=True)
    keep = np.r_[True, np.diff(t) > 0]
    t, df = t[keep], df[keep].reset_index(drop=True)

    A = df[["ax", "ay", "az"]].to_numpy(float)
    G = df[["gx", "gy", "gz"]].to_numpy(float)
    a_raw_mag = np.linalg.norm(A, axis=1)
    med_dt = float(np.median(np.diff(t)))
    hz = 1.0 / med_dt

    # ---- gravity mode: decided loudly, per trip
    med_mag = float(np.median(a_raw_mag))
    if med_mag > GRAVITY_DETECT_MS2:
        gravity_mode = "included"
        # per-frame gravity = low-passed accel (the classic virtual-reorientation
        # first step); horizontal = component perpendicular to it
        A_lp = _per_segment(t, A, hz, SUSTAINED_CUTOFF_HZ)
        g_vec = _per_segment(t, A, hz, 0.1)          # slower LP isolates gravity
        g_norm = np.linalg.norm(g_vec, axis=1, keepdims=True)
        g_hat = g_vec / np.clip(g_norm, 1e-6, None)
        lin = A_lp - g_vec                            # sustained linear accel
        vert = np.sum(lin * g_hat, axis=1, keepdims=True) * g_hat
        A_sus = lin - vert
        a_horiz = np.linalg.norm(A_sus, axis=1)
    else:
        gravity_mode = "removed"
        # OS already removed gravity every frame; low-passed magnitude of the
        # remaining linear accel is the sustained horizontal magnitude
        A_sus = _per_segment(t, A, hz, SUSTAINED_CUTOFF_HZ)
        a_horiz = np.linalg.norm(A_sus, axis=1)

    # ---- handling mask from sustained rotation rate
    G_lp = _per_segment(t, G, hz, HANDLING_LP_HZ)
    gyro_mag = np.linalg.norm(G_lp, axis=1)
    handling = gyro_mag > HANDLING_GYRO_DPS
    if handling.any():                               # pad by HANDLING_PAD_S
        n_pad = int(round(HANDLING_PAD_S * hz))
        idx = np.where(handling)[0]
        for i in idx:
            handling[max(0, i - n_pad): i + n_pad] = True

    print(f"[{key}] motion: {len(t):,} samples @ {hz:.0f} Hz, "
          f"{t[-1]/60:.1f} min, gravity={gravity_mode} (median |a|={med_mag:.2f}), "
          f"handling-masked {100*handling.mean():.1f}%")

    return MotionTrip(
        key=key, path=path, meta=meta, gravity_mode=gravity_mode,
        t=t, a_horiz=a_horiz, A_sus=A_sus, a_raw_mag=a_raw_mag,
        G_raw=G, G_lp=G_lp, gyro_mag=gyro_mag, handling=handling, hz=hz,
        segments=_segments(t), t0=t0,
    )


# ------------------------------------------------------------------- GPS

def _geodesic_cumdist(lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    """Cumulative path length (m), equirectangular per step (city scale)."""
    la = np.radians(lat)
    lo = np.radians(lon)
    dlat = np.diff(la)
    dlon = np.diff(lo) * np.cos((la[:-1] + la[1:]) / 2)
    step = 6_371_000.0 * np.hypot(dlat, dlon)
    return np.r_[0.0, np.cumsum(step)]


def load_gps(trip: MotionTrip) -> pd.DataFrame:
    """Phone GPS on the motion clock: raw speed + LOCREG-PCHIP v(t), a(t).

    The speed/accel curve needs no map: LOCREG-PCHIP on (t, cumulative path
    length) gives v = f', a = f'' exactly as the pipeline does on
    route-projected distance. GPS noise is bounded by the accuracy filter and
    the ~15 s LOCREG window.
    """
    df = pd.read_csv(trip.path / "pings.csv", dtype=str, keep_default_na=False)
    ts = pd.to_datetime(df["avl_event_time"])
    g = pd.DataFrame({
        "t": (ts - trip.t0).dt.total_seconds(),
        "lat": df["latitude"].astype(float),
        "lon": df["longitude"].astype(float),
        "accuracy_m": pd.to_numeric(df.get("accuracy_m", np.nan), errors="coerce"),
        "speed_mps": pd.to_numeric(df.get("speed", df.get("speed_mps", np.nan)),
                                   errors="coerce"),
    }).sort_values("t")
    g = g[(g.accuracy_m.isna()) | (g.accuracy_m <= GPS_MAX_ACC_M)]
    g = g.drop_duplicates("t").reset_index(drop=True)
    if len(g) < 30:
        raise ValueError(
            f"trip {trip.key}: only {len(g)} usable GPS pings — no GPS "
            f"speed/accel comparison possible for this trip")

    d = _geodesic_cumdist(g.lat.to_numpy(), g.lon.to_numpy())
    sm = locreg_pchip(g.t.to_numpy(), d, bandwidth=GPS_LOCREG_BW)
    f = sm.f
    tt = g.t.to_numpy()
    inside = (tt >= f.x[0]) & (tt <= f.x[-1])
    v = np.full(len(g), np.nan)
    a = np.full(len(g), np.nan)
    v[inside] = np.clip(f.derivative()(tt[inside]), 0, None)
    a[inside] = f.derivative(2)(tt[inside])
    g["v_recon_mps"] = v
    g["a_recon_ms2"] = a
    print(f"[{trip.key}] gps: {len(g):,} pings, "
          f"{100 * g.speed_mps.notna().mean():.0f}% with device speed")
    trip.gps = g
    return g
