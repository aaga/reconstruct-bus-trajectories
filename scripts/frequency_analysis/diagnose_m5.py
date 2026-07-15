"""Why does M5 (attribution agreement) fall off so fast?

Decomposes the weighted-Jaccard disagreement at three scopes:

  total    — scalar agreement on *how many* delay seconds exist at all
  category — agreement on delay seconds per category (dwell/signal/…),
             ignoring which facility they were pinned to
  facility — the full M5 key (category, facility)

The gaps between scopes separate three failure modes:
  100 - total    : wrong amount of detected delay (missed/invented seconds)
  total - category: right amount, wrong *category*
  category - facility: right category, wrong *facility* (e.g. neighbouring stop)

Also emits per-category detail (agreement, bench/var seconds, event counts)
and per-trip facility-scope values by route.

    PYTHONPATH=src uv run python scripts/frequency_analysis/diagnose_m5.py
"""

from __future__ import annotations

import pandas as pd

import config as C  # noqa: E402
import metrics as M  # noqa: E402
import pipeline as P  # noqa: E402
import trip_index as TI  # noqa: E402


def _jaccard_parts(b: dict, v: dict, keyfn) -> tuple[float, float]:
    bb: dict = {}
    vv: dict = {}
    for k, s in b.items():
        bb[keyfn(k)] = bb.get(keyfn(k), 0.0) + s
    for k, s in v.items():
        vv[keyfn(k)] = vv.get(keyfn(k), 0.0) + s
    keys = set(bb) | set(vv)
    s_min = sum(min(bb.get(k, 0.0), vv.get(k, 0.0)) for k in keys)
    s_max = sum(max(bb.get(k, 0.0), vv.get(k, 0.0)) for k in keys)
    return s_min, s_max


def main() -> int:
    trips = TI.load_cached_trips()
    rows = []
    for i, trip in enumerate(trips, 1):
        bench = P.run_level(trip, 0)
        for level in range(C.N_LEVELS):
            var = bench if level == 0 else P.run_level(trip, level)
            g = M.common_grid(bench, var)
            if g is None:
                continue
            t_lo, t_hi = g[0], g[1]
            b = M._attr_seconds(bench.attributions, t_lo, t_hi)
            v = M._attr_seconds(var.attributions, t_lo, t_hi)

            for scope, keyfn in (
                ("facility", lambda k: k),
                ("category", lambda k: k[0]),
                ("total", lambda k: "all"),
            ):
                s_min, s_max = _jaccard_parts(b, v, keyfn)
                rows.append(dict(trip_key=trip.trip_key, route_id=trip.route_id,
                                 level=level, scope=scope, s_min=s_min, s_max=s_max))

            # per-category detail at facility scope
            cats = {k[0] for k in set(b) | set(v)}
            for cat in cats:
                bk = {k: s for k, s in b.items() if k[0] == cat}
                vk = {k: s for k, s in v.items() if k[0] == cat}
                s_min, s_max = _jaccard_parts(bk, vk, lambda k: k)
                rows.append(dict(
                    trip_key=trip.trip_key, route_id=trip.route_id,
                    level=level, scope=f"cat:{cat}", s_min=s_min, s_max=s_max,
                    bench_sec=sum(bk.values()), var_sec=sum(vk.values()),
                    n_bench=sum(1 for a in bench.attributions if a["category"] == cat),
                    n_var=sum(1 for a in var.attributions if a["category"] == cat),
                ))
        if i % 25 == 0:
            print(f"  {i}/{len(trips)}")

    df = pd.DataFrame(rows)
    df.to_csv(C.RESULTS_DIR / "m5_diagnostics.csv", index=False)

    # ---- pooled scope ladder
    lad = (df[df.scope.isin(["facility", "category", "total"])]
           .groupby(["level", "scope"])[["s_min", "s_max"]].sum())
    lad["agree_pct"] = 100 * lad.s_min / lad.s_max
    print("\n=== scope ladder (pooled agreement %) ===")
    print(lad["agree_pct"].unstack()[["total", "category", "facility"]].round(1).to_string())

    # ---- per-category pooled
    catd = (df[df.scope.str.startswith("cat:")]
            .groupby(["level", "scope"])[["s_min", "s_max", "bench_sec", "var_sec",
                                          "n_bench", "n_var"]].sum())
    catd["agree_pct"] = 100 * catd.s_min / catd.s_max
    print("\n=== per-category (pooled) ===")
    with pd.option_context("display.width", 200):
        print(catd.round(1).to_string())

    # ---- per-route facility-scope agreement
    rt = (df[df.scope == "facility"]
          .groupby(["route_id", "level"])[["s_min", "s_max"]].sum())
    rt["agree_pct"] = (100 * rt.s_min / rt.s_max).round(1)
    print("\n=== facility-scope agreement by route ===")
    print(rt["agree_pct"].unstack().to_string())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
