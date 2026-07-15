"""Run the full ping-frequency sensitivity analysis.

For every QC'd trip: reconstruct + decompose the benchmark (2 s) and the seven
successively-halved datasets (4 … 256 s), compute all agreement metrics, and
write per-trip components + pooled summaries to outputs/frequency_analysis/.

    PYTHONPATH=src uv run python scripts/frequency_analysis/run_analysis.py
        [--rebuild-trips]  rebuild the trip cache first
        [--limit N]        only the first N trips (smoke test)
"""

from __future__ import annotations

import argparse
import json
import time
import traceback

import pandas as pd

import config as C  # noqa: E402
import metrics as M  # noqa: E402
import pipeline as P  # noqa: E402
import trip_index as TI  # noqa: E402


def analyze_trip(trip) -> tuple[list[dict], dict] | None:
    """All-level metric components for one trip, or None if benchmark fails."""
    try:
        bench = P.run_level(trip, 0)
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ {trip.trip_key}: benchmark failed ({e})")
        return None

    rows: list[dict] = []
    level_info: dict = {}
    for level in range(C.N_LEVELS):
        try:
            var = bench if level == 0 else P.run_level(trip, level)
        except Exception as e:  # noqa: BLE001
            level_info[level] = {"ok": False, "err": str(e)}
            continue
        comp = M.compute_all(bench, var, trip)
        comp.update(
            trip_key=trip.trip_key, level=level, route_id=trip.route_id,
            bus_id=trip.bus_id, shape_id=trip.shape_id,
            cadence_s=var.cadence_s, bandwidth=var.bandwidth,
            n_pings=var.n_pings, n_on_route=var.n_on_route,
        )
        rows.append(comp)
        level_info[level] = {"ok": True}
    return rows, level_info


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rebuild-trips", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    C.RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    if args.rebuild_trips or not (C.CACHE_DIR / "trips.json").exists():
        print("building trip index …")
        TI.cache_trips(TI.build_trips())
    trips = TI.load_cached_trips()
    if args.limit:
        trips = trips[: args.limit]
    print(f"analyzing {len(trips)} trips × {C.N_LEVELS} levels\n")

    all_rows: list[dict] = []
    status: dict[str, dict] = {}
    t_start = time.time()
    for i, trip in enumerate(trips, 1):
        try:
            res = analyze_trip(trip)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            res = None
        if res is None:
            status[trip.trip_key] = {"benchmark_ok": False}
            continue
        rows, level_info = res
        all_rows.extend(rows)
        status[trip.trip_key] = {
            "benchmark_ok": True,
            "levels_ok": sorted(l for l, v in level_info.items() if v["ok"]),
            "errors": {l: v["err"] for l, v in level_info.items() if not v["ok"]},
        }
        n_ok = len(status[trip.trip_key]["levels_ok"])
        print(f"[{i}/{len(trips)}] {trip.trip_key} rt {trip.route_id}: "
              f"{n_ok}/{C.N_LEVELS} levels ok "
              f"({time.time() - t_start:.0f}s elapsed)")

    comp = pd.DataFrame(all_rows)
    comp.to_csv(C.RESULTS_DIR / "per_trip_components.csv", index=False)
    (C.RESULTS_DIR / "trip_status.json").write_text(json.dumps(status, indent=1))

    # --- pooled summary over trips complete at ALL levels (same trip set at
    # every point of the curve — no composition drift across the x-axis).
    complete_keys = [k for k, v in status.items()
                     if v.get("benchmark_ok") and len(v.get("levels_ok", [])) == C.N_LEVELS]
    full = comp[comp.trip_key.isin(complete_keys)]
    summary = M.summarize(full)
    # attach nominal cadence + success-rate columns
    n_bench_ok = sum(1 for v in status.values() if v.get("benchmark_ok"))
    summary["nominal_cadence_s"] = [C.level_cadence_s(l) for l in summary["level"]]
    summary["n_trips_pooled"] = len(complete_keys)
    summary["recon_success_rate"] = [
        sum(1 for v in status.values() if l in v.get("levels_ok", [])) / max(n_bench_ok, 1)
        for l in summary["level"]
    ]
    summary.to_csv(C.RESULTS_DIR / "summary.csv", index=False)

    # --- per-trip metric values (for spread bands in plots)
    per_trip = full.copy()
    per_trip["m1"] = 100 * per_trip.pos_ok / per_trip.n_sec
    per_trip["m2"] = 100 * per_trip.spd_ok / per_trip.n_sec
    per_trip["m3"] = 100 * 2 * per_trip.tp / (2 * per_trip.tp + per_trip.fp + per_trip.fn)
    per_trip["m4"] = 100 * 2 * per_trip.n_match_ev / (per_trip.n_bench_ev + per_trip.n_var_ev)
    per_trip["m5"] = 100 * per_trip.attr_min_sum / per_trip.attr_max_sum
    per_trip["m6"] = 100 * per_trip.door_slow / per_trip.door_sec
    per_trip["m7"] = 100 * per_trip.dwell_match_id / per_trip.dwell_targets
    per_trip["m7p"] = 100 * per_trip.dwell_pred_ok / per_trip.dwell_pred
    per_trip = per_trip.replace([float("inf"), float("-inf")], float("nan"))
    per_trip.to_csv(C.RESULTS_DIR / "per_trip_metrics.csv", index=False)

    print(f"\npooled over {len(complete_keys)} fully-complete trips "
          f"(of {n_bench_ok} with a benchmark, {len(trips)} candidates)")
    print(summary.round(2).to_string(index=False))
    print(f"\nwrote {C.RESULTS_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
