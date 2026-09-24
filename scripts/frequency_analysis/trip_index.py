"""Build the trip index: R2 trip windows -> complete VTRAK trips -> cache.

Trip windows come from the R2 BusTime archive (``agency=cta``): per vehicle,
a window is the span of consecutive archive rows carrying the same
``(trip_id, route_id, start_date)``. A trip is **complete** when the VTRAK
2 s stream fully covers the window — first/last ping within
``MAX_VTRAK_GAP_S`` of the window edges and no internal gap larger than that.
(Chosen per 2026-08-11 discussion: "R2 span + VTRAK only".)

Each kept trip is map-matched once at 2 s to its GTFS shape
(``analysis.comparison``), giving every ping a distance-along-route ``x_m``
(cleaned to be monotone, paper section 3.1 style) and a measured speed
``v_mps``. Downsampled feeds are row-subsets of the cached pings, selected by
``stream_idx % stride == 0`` where ``stream_idx`` indexes the vehicle's full
continuous 2 s stream — so "downsample the stream, then trim to trips" is
reproduced exactly.

Run standalone to (re)build the cache:
    uv run python scripts/frequency_analysis/trip_index.py
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

import config as C  # noqa: E402  (sys.path set up in config)

from analysis.comparison import choose_shape, route_shape_map  # noqa: E402

MPH = 1.0 / C.MPS_TO_MPH  # m/s per mph

# Paper section 3.1 outlier rules. The paper's 500 ft forward-jump bound
# encodes "implied speed > 45 mph" at their cadence, so we apply the speed
# form directly (cadence-independent — the R2 reference feed is ~25 s).
MAX_FWD_MPS = 45.0 / 2.23694   # forward jump beyond this implied speed -> drop
MAX_BACK_M = 61.0              # 200 ft: larger backward jumps -> drop; smaller -> clamp


# ------------------------------------------------------------- VTRAK stream

def load_vtrak_stream() -> pd.DataFrame:
    """Deduped continuous 2 s stream for the study vehicles.

    ``dtime`` is the device wall clock (naive Chicago); the parquet
    ``timestamp`` is only the scraper poll time. ``stream_idx`` numbers each
    vehicle's pings 0..n-1 in time order — the downsampling index.
    """
    cache = C.CACHE_DIR / "vtrak_stream.parquet"
    if cache.exists():
        return pd.read_parquet(cache)
    frames = []
    for f in sorted(C.HIGHFREQ_DIR.glob("*/*.parquet")):
        df = pd.read_parquet(
            f, columns=["veH_ID", "dtime", "latitude", "longitude", "speed"])
        df = df[df["veH_ID"].astype(str).isin(C.VEHICLES)]
        if len(df):
            frames.append(df)
    df = pd.concat(frames, ignore_index=True)
    df["veh_id"] = df["veH_ID"].astype(int).astype(str)
    df["ping_dt"] = pd.to_datetime(df["dtime"], format="%m-%d-%Y %H:%M:%S")
    df = (
        df.drop_duplicates(["veh_id", "ping_dt"])
        .sort_values(["veh_id", "ping_dt"])
        .reset_index(drop=True)
    )
    df["v_mps"] = pd.to_numeric(df["speed"], errors="coerce") * MPH
    df["stream_idx"] = df.groupby("veh_id").cumcount()
    out = df[["veh_id", "stream_idx", "ping_dt", "latitude", "longitude", "v_mps"]]
    C.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out.to_parquet(cache)
    return out


# ------------------------------------------------------------- R2 windows

def load_r2_rows() -> pd.DataFrame:
    """R2 archive rows for the study vehicles, in naive Chicago time."""
    cache = C.CACHE_DIR / "r2_rows.parquet"
    if cache.exists():
        return pd.read_parquet(cache)
    files = sorted(C.R2_DIR.glob(f"agency={C.R2_AGENCY}__year=*.parquet"))
    frames = []
    for f in files:
        df = pd.read_parquet(
            f, columns=["vehicle_id", "trip_id", "route_id", "start_date", "timestamp"])
        df = df[df["vehicle_id"].astype(str).isin(C.VEHICLES)]
        if len(df):
            frames.append(df)
    df = pd.concat(frames, ignore_index=True)
    df["vehicle_id"] = df["vehicle_id"].astype(str)
    ts = pd.to_datetime(df["timestamp"], utc=True)
    df["ts"] = ts.dt.tz_convert(C.LOCAL_TZ).dt.tz_localize(None)
    df = (
        df.drop_duplicates(["vehicle_id", "ts"])
        .sort_values(["vehicle_id", "ts"])
        .reset_index(drop=True)
    )
    out = df[["vehicle_id", "trip_id", "route_id", "start_date", "ts"]]
    C.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out.to_parquet(cache)
    return out


def r2_trip_windows(r2: pd.DataFrame, max_r2_gap_s: float = 1800.0) -> pd.DataFrame:
    """One row per consecutive same-trip run of R2 observations."""
    r2 = r2.dropna(subset=["trip_id", "route_id"]).copy()
    r2["trip_id"] = r2["trip_id"].astype(str)
    r2["route_id"] = r2["route_id"].astype(str)
    r2 = r2[(r2["trip_id"] != "") & (r2["route_id"] != "")]

    rows = []
    for veh, g in r2.groupby("vehicle_id"):
        g = g.sort_values("ts")
        key = g["trip_id"] + "|" + g["route_id"] + "|" + g["start_date"].astype(str)
        gap = g["ts"].diff().dt.total_seconds().fillna(0)
        new_run = (key != key.shift()) | (gap > max_r2_gap_s)
        for _, run in g.groupby(new_run.cumsum()):
            rows.append({
                "veh_id": veh,
                "trip_id": run["trip_id"].iloc[0],
                "route_id": run["route_id"].iloc[0],
                "start_date": str(run["start_date"].iloc[0]),
                "t_lo": run["ts"].iloc[0],
                "t_hi": run["ts"].iloc[-1],
                "n_r2": len(run),
            })
    w = pd.DataFrame(rows)
    w["duration_s"] = (w["t_hi"] - w["t_lo"]).dt.total_seconds()
    w["trip_key"] = (
        w["veh_id"] + "_" + w["trip_id"].str.replace(r"\W", "", regex=True)
        + "_" + w["t_lo"].dt.strftime("%Y%m%d%H%M%S")
    )
    return w.sort_values(["veh_id", "t_lo"]).reset_index(drop=True)


# ------------------------------------------------------------- x cleaning

def clean_dist_along(t_s: np.ndarray, x: np.ndarray,
                     on_route: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Paper-style outlier pass on matched distance-along-route.

    Returns (monotone x, keep mask). Points that are off-route, imply a
    forward speed beyond MAX_FWD_MPS vs the running front, or backtrack
    more than MAX_BACK_M are dropped; remaining small backtracks are
    clamped forward (running max) so x is non-decreasing. ``t_s`` are the
    ping times in seconds (any epoch), used for the implied-speed rule.
    """
    n = len(x)
    keep = np.zeros(n, dtype=bool)
    front = -np.inf
    t_front = 0.0
    for i in range(n):
        if not on_route[i] or not np.isfinite(x[i]):
            continue
        if front == -np.inf:
            keep[i] = True
            front, t_front = x[i], t_s[i]
            continue
        dt = max(t_s[i] - t_front, 1.0)
        if x[i] - front > MAX_FWD_MPS * dt or front - x[i] > MAX_BACK_M:
            continue
        keep[i] = True
        if x[i] > front:
            front, t_front = x[i], t_s[i]
    xk = np.maximum.accumulate(x[keep])
    return xk, keep


# ------------------------------------------------------------- build

@dataclass
class Trip:
    trip_key: str
    veh_id: str
    trip_id: str
    route_id: str
    shape_id: str
    pings: pd.DataFrame          # stream_idx, ping_dt, x_m, v_mps, latitude, longitude
    doors: pd.DataFrame          # event_dt, dwell_s, stop_id, event_type, x_door_m
    signal_x: np.ndarray = field(default_factory=lambda: np.array([]))
    qc: dict = field(default_factory=dict)


def load_door_csv() -> pd.DataFrame:
    df = pd.read_csv(C.DOOR_CSV, dtype=str, keep_default_na=False)
    df["event_dt"] = pd.to_datetime(df["event_time"], format="%Y-%m-%d %H:%M:%S.%f")
    df["dwell_s"] = pd.to_numeric(df["dwell_time"], errors="coerce").fillna(0.0).clip(lower=0)
    df["bus_id"] = df["bus_id"].astype(str)
    # AVL-reported door location; 0.0 means "no fix" -> NaN
    for c in ("latitude", "longitude"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
        df.loc[df[c] == 0.0, c] = np.nan
    return df


def build_trips(verbose: bool = True) -> list[Trip]:
    stream = load_vtrak_stream()
    windows = r2_trip_windows(load_r2_rows())
    avl = load_door_csv()
    route_shapes = route_shape_map(C.GTFS)
    from analysis.comparison import matcher_for
    from dataio.intersections import load_intersections
    from core.control_points import SIGNALIZED_CONTROL_TYPES
    intersections = load_intersections(C.INTERSECTIONS)

    out: list[Trip] = []
    skipped: dict[str, int] = {}

    def skip(reason: str):
        skipped[reason] = skipped.get(reason, 0) + 1

    for w in windows.itertuples(index=False):
        if not (C.MIN_TRIP_S <= w.duration_s <= C.MAX_TRIP_S):
            skip("bad_duration")
            continue
        s = stream[stream["veh_id"] == w.veh_id]
        g = s[(s["ping_dt"] >= w.t_lo) & (s["ping_dt"] <= w.t_hi)].reset_index(drop=True)
        if len(g) < C.MIN_PINGS:
            skip("too_few_pings")
            continue

        # completeness: full VTRAK coverage of the R2 window
        t = g["ping_dt"]
        edge_lo = (t.iloc[0] - w.t_lo).total_seconds()
        edge_hi = (w.t_hi - t.iloc[-1]).total_seconds()
        max_gap = t.diff().dt.total_seconds().iloc[1:].max()
        if edge_lo > C.MAX_VTRAK_GAP_S or edge_hi > C.MAX_VTRAK_GAP_S \
                or max_gap > C.MAX_VTRAK_GAP_S:
            skip("incomplete_vtrak")
            continue

        shape_id = choose_shape(
            g[["latitude", "longitude"]], w.route_id, route_shapes, hint_shape="")
        if not shape_id:
            skip("no_shape")
            continue
        try:
            res = matcher_for(shape_id).match(
                g["latitude"].to_numpy(), g["longitude"].to_numpy())
        except KeyError:
            skip("shape_not_in_gtfs")
            continue
        frac = float(res.on_route.mean())
        if frac < C.MIN_ON_ROUTE_FRAC:
            skip("poor_match")
            continue

        t_s = g["ping_dt"].astype("datetime64[ns]").astype("int64").to_numpy() / 1e9
        x_clean, keep = clean_dist_along(t_s, res.dist_along_m, res.on_route)
        g = g[keep].reset_index(drop=True)
        g["x_m"] = x_clean
        if len(g) < C.MIN_PINGS:
            skip("too_few_pings_after_clean")
            continue
        net = float(g["x_m"].iloc[-1] - g["x_m"].iloc[0])
        if net < C.MIN_FORWARD_M:
            skip("short_forward")
            continue

        doors = avl[
            (avl["bus_id"] == w.veh_id)
            & avl["event_type"].isin(C.DOOR_EVENT_TYPES)
            & (avl["event_dt"] >= w.t_lo)
            & (avl["event_dt"] <= w.t_hi)
        ][["event_dt", "dwell_s", "stop_id", "event_type",
           "latitude", "longitude"]].reset_index(drop=True)

        # AVL door location -> distance along the same shape (M3b)
        doors["x_door_m"] = np.nan
        has_ll = doors["latitude"].notna() & doors["longitude"].notna()
        if has_ll.any():
            dres = matcher_for(shape_id).match(
                doors.loc[has_ll, "latitude"].to_numpy(),
                doors.loc[has_ll, "longitude"].to_numpy())
            xd = np.where(dres.on_route, dres.dist_along_m, np.nan)
            doors.loc[has_ll, "x_door_m"] = xd

        # signalized control points within the trip's traversed x range (M5)
        x_lo, x_hi = float(g["x_m"].iloc[0]), float(g["x_m"].iloc[-1])
        sig = np.array(sorted(
            cp.dist_along_route_m for cp in intersections.get(shape_id, [])
            if cp.control_type in SIGNALIZED_CONTROL_TYPES
            and x_lo + C.SIGNAL_ZONE_M <= cp.dist_along_route_m <= x_hi
        ))

        out.append(Trip(
            trip_key=w.trip_key, veh_id=w.veh_id, trip_id=w.trip_id,
            route_id=w.route_id, shape_id=shape_id,
            pings=g[["stream_idx", "ping_dt", "x_m", "v_mps", "latitude", "longitude"]],
            doors=doors,
            signal_x=sig,
            qc={"n_pings": len(g), "on_route_frac": round(frac, 3),
                "net_forward_m": round(net, 1),
                "duration_s": round(w.duration_s, 0),
                "n_door_events": len(doors),
                "kept_frac": round(float(keep.mean()), 3)},
        ))
        if verbose and len(out) % 50 == 0:
            print(f"  ... {len(out)} trips kept so far")

    if verbose:
        print(f"\n{len(out)} complete trips kept; skipped: {skipped}")
    return out


# ------------------------------------------------------------- cache

def cache_trips(trips: list[Trip]) -> None:
    C.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    meta, ping_frames, door_frames, sig_frames = [], [], [], []
    for tr in trips:
        meta.append({
            "trip_key": tr.trip_key, "veh_id": tr.veh_id, "trip_id": tr.trip_id,
            "route_id": tr.route_id, "shape_id": tr.shape_id, **tr.qc,
        })
        p = tr.pings.copy()
        p["trip_key"] = tr.trip_key
        ping_frames.append(p)
        d = tr.doors.copy()
        d["trip_key"] = tr.trip_key
        door_frames.append(d)
        sig_frames.append(pd.DataFrame(
            {"trip_key": tr.trip_key, "x_sig_m": tr.signal_x}))
    (C.CACHE_DIR / "trips.json").write_text(json.dumps(meta, indent=1))
    pd.concat(ping_frames, ignore_index=True).to_parquet(C.CACHE_DIR / "trip_pings.parquet")
    pd.concat(door_frames, ignore_index=True).to_parquet(C.CACHE_DIR / "trip_doors.parquet")
    pd.concat(sig_frames, ignore_index=True).to_parquet(C.CACHE_DIR / "trip_signals.parquet")
    print(f"cached {len(trips)} trips -> {C.CACHE_DIR}")


def load_cached_trips() -> list[Trip]:
    meta = json.loads((C.CACHE_DIR / "trips.json").read_text())
    pings = pd.read_parquet(C.CACHE_DIR / "trip_pings.parquet")
    doors = pd.read_parquet(C.CACHE_DIR / "trip_doors.parquet")
    sigs = pd.read_parquet(C.CACHE_DIR / "trip_signals.parquet")
    qc_keys = ("n_pings", "on_route_frac", "net_forward_m", "duration_s",
               "n_door_events", "kept_frac")
    out = []
    for m in meta:
        k = m["trip_key"]
        out.append(Trip(
            trip_key=k, veh_id=m["veh_id"], trip_id=m["trip_id"],
            route_id=m["route_id"], shape_id=m["shape_id"],
            pings=pings[pings["trip_key"] == k].drop(columns="trip_key").reset_index(drop=True),
            doors=doors[doors["trip_key"] == k].drop(columns="trip_key").reset_index(drop=True),
            signal_x=sigs.loc[sigs["trip_key"] == k, "x_sig_m"].to_numpy(),
            qc={x: m[x] for x in qc_keys},
        ))
    return out


if __name__ == "__main__":
    cache_trips(build_trips())
