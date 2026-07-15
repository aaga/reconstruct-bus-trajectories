"""Build the trip index: AVL door-event trips -> dense highfreq ping slices.

The dense VTRAK parquet stream has no trip/route labels. The door-events CSV
(``bus_state_hist_highfreq-VTRAK_*.csv``) does: every AVL event row carries
``(bus_id, trip_id, trip_start_time, route_id)``. We use those to cut the
continuous per-vehicle ping stream into revenue trips, then pick the GTFS
shape each trip actually follows (reusing ``analysis.comparison.choose_shape``)
and QC the result.

Everything is kept in **naive America/Chicago wall time** — the native clock
of both the VTRAK ``dtime`` field and the AVL ``event_time`` field (June has
no DST transition).

Run standalone to (re)build the cache:
    PYTHONPATH=src uv run python scripts/frequency_analysis/trip_index.py
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

import config as C  # noqa: E402  (sys.path set up in config)

from analysis.comparison import choose_shape, route_shape_map  # noqa: E402

DOOR_EVENT_TYPES = {"3", "4", "5"}  # Serviced / UnServiced / Unknown Stop


# ------------------------------------------------------------- door events

def load_door_csv() -> pd.DataFrame:
    """The full AVL dump for the three vehicles, with parsed times."""
    df = pd.read_csv(C.DOOR_CSV, dtype=str, keep_default_na=False)
    df["event_dt"] = pd.to_datetime(df["event_time"], format="%Y-%m-%d %H:%M:%S.%f")
    df["dwell_s"] = pd.to_numeric(df["dwell_time"], errors="coerce").fillna(0.0).clip(lower=0)
    return df


def trip_table(avl: pd.DataFrame) -> pd.DataFrame:
    """One row per (bus_id, trip_id, trip_start_time) revenue trip.

    The window spans every AVL event the trip produced (door events and
    otherwise); route_id is the modal route among its rows.
    """
    ok = (
        (avl["trip_id"] != "") & (avl["trip_id"] != "None")
        & (avl["trip_start_time"] != "") & (avl["trip_start_time"] != "None")
        & avl["route_id"].map(C.is_revenue_route)
    )
    sub = avl[ok]
    rows = []
    for (bus, trip, start), g in sub.groupby(["bus_id", "trip_id", "trip_start_time"]):
        rows.append({
            "bus_id": bus,
            "trip_id": trip,
            "trip_start_time": start,
            "route_id": g["route_id"].mode().iloc[0],
            "t_lo": g["event_dt"].min(),
            "t_hi": g["event_dt"].max(),
            "n_events": len(g),
            "n_door": int(g["event_type"].isin(DOOR_EVENT_TYPES).sum()),
        })
    t = pd.DataFrame(rows).sort_values(["bus_id", "t_lo"]).reset_index(drop=True)
    t["trip_key"] = (
        t["bus_id"] + "_" + t["trip_id"] + "_"
        + t["t_lo"].dt.strftime("%Y%m%d%H%M%S")
    )
    t["duration_s"] = (t["t_hi"] - t["t_lo"]).dt.total_seconds()
    return t


# ------------------------------------------------------------- dense pings

def load_highfreq_pings() -> pd.DataFrame:
    """All dense VTRAK pings, deduped per vehicle on device time (``dtime``).

    ``dtime`` is the device-report wall clock (1 s resolution, ~2 s cadence);
    the parquet ``timestamp`` is merely the scraper's poll time and would add
    up to ~2 s of sampling jitter, so ``dtime`` is the ping timestamp.
    """
    files = sorted(C.HIGHFREQ_DIR.glob("*/*.parquet"))
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    df["veh_id"] = df["veH_ID"].astype(int).astype(str)
    df["ping_dt"] = pd.to_datetime(df["dtime"], format="%m-%d-%Y %H:%M:%S")
    df = (
        df.drop_duplicates(["veh_id", "ping_dt"])
        .sort_values(["veh_id", "ping_dt"])
        .reset_index(drop=True)
    )
    return df[["veh_id", "ping_dt", "latitude", "longitude", "heading", "speed"]]


# ------------------------------------------------------------- trip slicing

@dataclass
class Trip:
    trip_key: str
    bus_id: str
    trip_id: str
    route_id: str
    shape_id: str
    t0: pd.Timestamp            # wall clock of first kept ping (naive Chicago)
    pings: pd.DataFrame         # columns: ping_dt, latitude, longitude
    door_events: pd.DataFrame   # this trip's AVL rows (door events, parsed)
    qc: dict = field(default_factory=dict)


def _trim_stationary_ends(g: pd.DataFrame, move_m: float = 30.0,
                          keep_s: float = 20.0) -> pd.DataFrame:
    """Trim terminal layover: drop leading/trailing spans where the bus hasn't
    moved more than ``move_m`` from its resting point, keeping ``keep_s`` of
    lead-in/out so the reconstruction still sees the stationary edge."""
    lat = np.radians(g["latitude"].to_numpy())
    lon = np.radians(g["longitude"].to_numpy())
    # meters from first / last position (equirectangular; fine at city scale)
    def dist_from(i0: int) -> np.ndarray:
        dlat = lat - lat[i0]
        dlon = (lon - lon[i0]) * np.cos(lat[i0])
        return 6_371_000.0 * np.hypot(dlat, dlon)

    moved = np.where(dist_from(0) > move_m)[0]
    i_lo = int(moved[0]) if len(moved) else 0
    moved = np.where(dist_from(len(g) - 1) > move_m)[0]
    i_hi = int(moved[-1]) if len(moved) else len(g) - 1

    t = g["ping_dt"]
    lo_t = t.iloc[i_lo] - pd.Timedelta(seconds=keep_s)
    hi_t = t.iloc[i_hi] + pd.Timedelta(seconds=keep_s)
    return g[(t >= lo_t) & (t <= hi_t)].reset_index(drop=True)


def build_trips(verbose: bool = True) -> list[Trip]:
    """Slice the dense stream into QC'd revenue trips with chosen shapes."""
    avl = load_door_csv()
    trips = trip_table(avl)
    pings = load_highfreq_pings()
    route_shapes = route_shape_map(C.GTFS)

    out: list[Trip] = []
    skipped: dict[str, int] = {}

    def skip(reason: str):
        skipped[reason] = skipped.get(reason, 0) + 1

    for row in trips.itertuples(index=False):
        if row.duration_s > C.MAX_TRIP_H * 3600 or row.duration_s < 300:
            skip("bad_duration")
            continue
        pad = pd.Timedelta(seconds=C.WINDOW_PAD_S)
        g = pings[
            (pings["veh_id"] == row.bus_id)
            & (pings["ping_dt"] >= row.t_lo - pad)
            & (pings["ping_dt"] <= row.t_hi + pad)
        ].reset_index(drop=True)
        if len(g) < C.MIN_PINGS:
            skip("too_few_pings")
            continue

        dt = g["ping_dt"].diff().dt.total_seconds().iloc[1:]
        if dt.median() > C.MAX_MEDIAN_CADENCE_S or dt.max() > C.MAX_GAP_S:
            skip("cadence_gap")
            continue

        g = _trim_stationary_ends(g)
        if len(g) < C.MIN_PINGS:
            skip("too_few_pings_after_trim")
            continue

        # Shape the pings actually follow (handles direction + GTFS variants).
        df_ll = pd.DataFrame({"latitude": g["latitude"], "longitude": g["longitude"]})
        shape_id = choose_shape(df_ll, row.route_id, route_shapes, hint_shape="")
        if not shape_id:
            skip("no_shape")
            continue

        # QC the match on the chosen shape.
        from analysis.comparison import matcher_for  # cached import
        try:
            res = matcher_for(shape_id).match(
                g["latitude"].to_numpy(), g["longitude"].to_numpy())
        except KeyError:
            skip("shape_not_in_gtfs")
            continue
        on = res.on_route
        frac = float(on.mean())
        net = float(res.dist_along_m[on][-1] - res.dist_along_m[on][0]) if on.sum() >= 2 else 0.0
        if frac < C.MIN_ON_ROUTE_FRAC or net < C.MIN_FORWARD_M:
            skip("poor_match")
            continue

        door = avl[
            (avl["bus_id"] == row.bus_id)
            & (avl["trip_id"] == row.trip_id)
            & (avl["trip_start_time"] == row.trip_start_time)
            & avl["event_type"].isin(DOOR_EVENT_TYPES)
        ].copy()

        out.append(Trip(
            trip_key=row.trip_key,
            bus_id=row.bus_id,
            trip_id=row.trip_id,
            route_id=row.route_id,
            shape_id=shape_id,
            t0=g["ping_dt"].iloc[0],
            pings=g,
            door_events=door,
            qc={"n_pings": len(g), "on_route_frac": round(frac, 3),
                "net_forward_m": round(net, 1),
                "median_cadence_s": float(dt.median())},
        ))
        if verbose:
            print(f"  ✓ {row.trip_key}: rt {row.route_id} shape {shape_id} "
                  f"{len(g)} pings, {frac:.0%} on-route, {net/1000:.1f} km")

    if verbose:
        print(f"\n{len(out)} trips kept; skipped: {skipped}")
    return out


# ------------------------------------------------------------- cache

def cache_trips(trips: list[Trip]) -> None:
    C.CACHE_DIR.mkdir(parents=True, exist_ok=True)
    meta = []
    ping_frames = []
    door_frames = []
    for tr in trips:
        meta.append({
            "trip_key": tr.trip_key, "bus_id": tr.bus_id, "trip_id": tr.trip_id,
            "route_id": tr.route_id, "shape_id": tr.shape_id,
            "t0": tr.t0.isoformat(), **tr.qc,
        })
        p = tr.pings.copy()
        p["trip_key"] = tr.trip_key
        ping_frames.append(p)
        d = tr.door_events.copy()
        d["trip_key"] = tr.trip_key
        door_frames.append(d)
    (C.CACHE_DIR / "trips.json").write_text(json.dumps(meta, indent=1))
    pd.concat(ping_frames, ignore_index=True).to_parquet(C.CACHE_DIR / "trip_pings.parquet")
    pd.concat(door_frames, ignore_index=True).to_parquet(C.CACHE_DIR / "trip_doors.parquet")
    print(f"cached {len(trips)} trips -> {C.CACHE_DIR}")


def load_cached_trips() -> list[Trip]:
    meta = json.loads((C.CACHE_DIR / "trips.json").read_text())
    pings = pd.read_parquet(C.CACHE_DIR / "trip_pings.parquet")
    doors = pd.read_parquet(C.CACHE_DIR / "trip_doors.parquet")
    out = []
    for m in meta:
        k = m["trip_key"]
        out.append(Trip(
            trip_key=k, bus_id=m["bus_id"], trip_id=m["trip_id"],
            route_id=m["route_id"], shape_id=m["shape_id"],
            t0=pd.Timestamp(m["t0"]),
            pings=pings[pings["trip_key"] == k].drop(columns="trip_key").reset_index(drop=True),
            door_events=doors[doors["trip_key"] == k].drop(columns="trip_key").reset_index(drop=True),
            qc={x: m[x] for x in ("n_pings", "on_route_frac", "net_forward_m", "median_cadence_s")},
        ))
    return out


if __name__ == "__main__":
    cache_trips(build_trips())
