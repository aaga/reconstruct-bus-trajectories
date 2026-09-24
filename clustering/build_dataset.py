"""Build the clustering corpus: R2 TransLink route-99 pings -> LOCREG-PCHIP
(bw=8) trajectories + space-aligned t(d) profile matrix.

Outputs (all under ``outputs/clustering/``):
  - ``trajectories_r99_wb_bw8.json``  serialized PCHIP records (core.serialize)
  - ``trip_meta_r99_wb.csv``          one row per kept trip: start local time,
                                      weekday, hour, vehicle, runtime, n_pings
  - ``profiles_r99_wb.npz``           d_grid (K,), T (N,K) seconds-to-reach
                                      matrix, trip_keys (N,)

Run:  PYTHONPATH=src uv run python clustering/build_dataset.py
"""

from __future__ import annotations

import json
import zipfile

import numpy as np
import pandas as pd

import config as C  # noqa: E402  (clustering/config.py inserts src on path)

from core.mapmatch.shape_snap import SnapToShapeMatcher  # noqa: E402
from core.reconstruct import reconstruct_trip  # noqa: E402
from core.serialize import to_pchip_record  # noqa: E402


def load_shape() -> tuple[np.ndarray, np.ndarray]:
    """(N,2) lat/lon polyline + per-vertex dist-along in metres (km->m)."""
    with zipfile.ZipFile(C.GTFS_ZIP) as z:
        shapes = pd.read_csv(z.open("shapes.txt"), dtype={"shape_id": str})
    s = (
        shapes[shapes.shape_id == C.SHAPE_ID]
        .sort_values("shape_pt_sequence")
        .reset_index(drop=True)
    )
    if s.empty:
        raise SystemExit(f"shape {C.SHAPE_ID} not in {C.GTFS_ZIP}")
    poly = s[["shape_pt_lat", "shape_pt_lon"]].to_numpy(float)
    dist_m = s.shape_dist_traveled.to_numpy(float) * 1000.0
    return poly, dist_m


def load_wb_trip_ids() -> set[str]:
    """Static trip_ids scheduled on the study shape (WB to UBC)."""
    with zipfile.ZipFile(C.GTFS_ZIP) as z:
        trips = pd.read_csv(z.open("trips.txt"), dtype=str)
    keep = trips[(trips.route_id == C.ROUTE_ID) & (trips.shape_id == C.SHAPE_ID)]
    return set(keep.trip_id)


def select_trips(
    pings: pd.DataFrame, matcher: SnapToShapeMatcher, shape_len_m: float
) -> list[tuple[str, pd.DataFrame]]:
    """QC-gate and terminal-truncate each (trip_id, start_date) ping group."""
    wb_ids = load_wb_trip_ids()
    pings = pings.drop_duplicates(["vehicle_id", "timestamp"]).copy()
    pings = pings[pings.trip_id.isin(wb_ids)]
    pings = pings.sort_values(["trip_id", "start_date", "timestamp"])

    stats = dict.fromkeys(
        ["too_few_pings", "not_at_origin", "no_terminal_reach",
         "gap_while_moving", "too_long", "span_too_small", "kept"], 0)
    kept: list[tuple[str, pd.DataFrame]] = []
    for (tid, sdate), g in pings.groupby(["trip_id", "start_date"]):
        if len(g) < C.MIN_PINGS:
            stats["too_few_pings"] += 1
            continue
        m = matcher.match(g.latitude.to_numpy(), g.longitude.to_numpy())
        d, on = m.dist_along_m, m.on_route
        if d[0] > C.ORIGIN_TOL_M:
            stats["not_at_origin"] += 1
            continue
        at_term = (d >= shape_len_m - C.TERM_TOL_M) & on
        if not at_term.any():
            stats["no_terminal_reach"] += 1
            continue
        end = int(np.argmax(at_term)) + 1
        trunc, d_tr = g.iloc[:end], d[:end]
        # Trim the leading origin dwell: vehicles broadcast the next trip_id
        # while parked on layover at the origin terminal and go silent while
        # stationary, so pre-departure silence would otherwise fail the gap
        # gate for data the profiles (which start at GRID_D0_M) never use.
        # Start the trip at the LAST ping still within 200 m of the shape start.
        pre = np.where(d_tr < 200.0)[0]
        s0 = int(pre[-1]) if len(pre) else 0
        trunc, d_tr = trunc.iloc[s0:], d_tr[s0:]
        if len(trunc) < C.MIN_PINGS:
            stats["too_few_pings"] += 1
            continue
        # Gap gate: only silence while MOVING is disqualifying. A stationary
        # gap (bus advanced < GAP_MOVE_M across it) is a hold, not data loss.
        gaps = trunc.timestamp.diff().dt.total_seconds().fillna(0).to_numpy()
        moved = np.abs(np.diff(d_tr, prepend=d_tr[0]))
        if ((gaps > C.GAP_MAX_S) & (moved > C.GAP_MOVE_M)).any():
            stats["gap_while_moving"] += 1
            continue
        dur_s = (trunc.timestamp.iloc[-1] - trunc.timestamp.iloc[0]).total_seconds()
        if not (0 < dur_s <= C.MAX_TRIP_H * 3600):
            stats["too_long"] += 1
            continue
        if (d_tr.max() - d_tr.min()) < C.MIN_SPAN_FRAC * shape_len_m:
            stats["span_too_small"] += 1
            continue
        kept.append((f"{tid}_{sdate}", trunc))
        stats["kept"] += 1

    print("Filter outcomes:")
    for k, v in stats.items():
        print(f"  {k:>18}: {v}")
    return kept


def main() -> int:
    C.OUT_DIR.mkdir(parents=True, exist_ok=True)
    poly, dist_m = load_shape()
    shape_len_m = float(dist_m[-1])
    matcher = SnapToShapeMatcher(
        polyline_latlon=poly,
        dist_along_m_per_vertex=dist_m,
        max_perp_m=C.MAX_PERP_M,
    )
    print(f"Shape {C.SHAPE_ID}: {shape_len_m:.0f} m")

    pings = pd.read_parquet(C.PINGS_PARQUET)
    print(f"Route pings: {len(pings):,}")
    trips = select_trips(pings, matcher, shape_len_m)

    records, meta_rows = [], []
    for key, g in trips:
        trip_df = pd.DataFrame({
            "avl_event_time": g.timestamp.dt.tz_convert(None),
            "latitude": g.latitude.to_numpy(float),
            "longitude": g.longitude.to_numpy(float),
            "trip_id": key,
            "bus_id": g.vehicle_id.astype(str),
            "route_id": C.ROUTE_ID,
            "pattern_id": C.SHAPE_ID,
        })
        try:
            recon = reconstruct_trip(
                trip_df, matcher, bandwidth=C.BANDWIDTH, degree=C.DEGREE)
        except Exception as e:  # noqa: BLE001 -- skip unreconstructable trips
            print(f"  ! {key}: {e}")
            continue
        records.append(to_pchip_record(recon))
        t0 = g.timestamp.iloc[0].tz_convert(C.TZ)
        meta_rows.append({
            "trip_key": key,
            "start_local": t0.isoformat(),
            "date": str(t0.date()),
            "weekday": t0.day_name(),
            "hour": t0.hour + t0.minute / 60,
            "vehicle_id": str(g.vehicle_id.iloc[0]),
            "n_pings": len(g),
            "runtime_s": (g.timestamp.iloc[-1] - g.timestamp.iloc[0]).total_seconds(),
        })

    C.TRAJ_JSON.write_text(json.dumps({"trips": records}, separators=(",", ":")))
    meta = pd.DataFrame(meta_rows)
    meta.to_csv(C.META_CSV, index=False)
    print(f"Reconstructed {len(records)} trips -> {C.TRAJ_JSON}")

    # ---- space-aligned profiles: seconds-to-reach t(d) on a common d grid.
    # f(t) is monotone, so invert by interpolating t against x. t is
    # re-anchored so t=0 at d_grid[0] (drops any pre-launch layover noise).
    from core.serialize import from_pchip_record  # noqa: E402

    d_grid = np.arange(C.GRID_D0_M, shape_len_m - C.TERM_TOL_M, C.GRID_STEP_M)
    T = np.full((len(records), len(d_grid)), np.nan)
    for i, rec in enumerate(records):
        f = from_pchip_record(rec)
        tt = np.linspace(f.x[0], f.x[-1], 4000)
        xx = f(tt)
        ok = np.isfinite(xx)
        tt, xx = tt[ok], xx[ok]
        xx = np.maximum.accumulate(xx)  # guard tiny non-monotone numerics
        if xx[0] > d_grid[0] or xx[-1] < d_grid[-1]:
            continue  # leaves NaN row; filtered below
        T[i] = np.interp(d_grid, xx, tt)
    T = T - T[:, [0]]

    good = ~np.isnan(T).any(axis=1)
    print(f"Profiles: {good.sum()}/{len(records)} trips cover the full grid")
    np.savez_compressed(
        C.PROFILE_NPZ,
        d_grid=d_grid,
        T=T[good],
        trip_keys=meta.trip_key.to_numpy()[good],
    )
    print(f"Wrote {C.PROFILE_NPZ}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
