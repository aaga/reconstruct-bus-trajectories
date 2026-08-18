"""Per-bus-stop performance stats (2026-08-05 feature).

For every stop_id:
  * door events:  count, total door-open seconds, p50 / p90 open time
                  (from the door cache directly — every passenger-activity
                  cycle, one row per cycle)
  * pre-dwell:    count, total seconds, p50 / p90  (events parquet, cls=pre)
  * post-dwell:   count, total seconds, p50 / p90  (cls in post/post2)

pre/post attribution uses the stop_id stamped on the piece by delay_events
(the associated door cycle's system stop attribution), so the numbers stay
consistent with the distribution view's classification rules.

Output: outputs/network/<city>/stop_stats.parquet (+ console summary).

Usage:
    PYTHONPATH=src uv run python analysis/network/build_stop_stats.py --city cta
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import duckdb

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from dataio.cities import get_city  # noqa: E402


def build(city_id: str) -> None:
    city = get_city(city_id)
    base = REPO / "outputs" / "network" / city.city_id
    ev_glob = str(base / "events" / "service_date=*" / "route=*.parquet")
    # Door source must span the same window as the events, or the per-stop
    # ratios silently mix scales: the 3-month cache against 2.5 years of
    # pre/post pieces made n_door look 10x too small (2026-08-18).
    if city.door_source_dir:
        door_glob = str(Path(city.door_source_dir) / "month=*.parquet")
    else:
        door_glob = str(city.resolve("caches/door_events") / city.city_id / "*.parquet")
    out = base / "stop_stats.parquet"

    # stop names from the registry (union across segments)
    reg = json.loads((base / "segment_registry.json").read_text())
    # name + signal-side classification (registry.py): near_side is within
    # SIDE_WINDOW_FT before the light (signal_dist_ft > 0), far_side within
    # that distance after it (signal_dist_ft < 0), else other (null distance).
    names: dict[str, tuple] = {}
    for rec in reg["segments"].values():
        for s in rec.get("stops_off", []):
            names.setdefault(str(s["id"]), (
                s["name"], s.get("signal_side"), s.get("signal_dist_ft")))

    con = duckdb.connect()
    con.execute("CREATE TABLE names(stop_id TEXT, stop_name TEXT, "
                "signal_side TEXT, signal_dist_ft DOUBLE)")
    if names:
        con.executemany("INSERT INTO names VALUES (?, ?, ?, ?)",
                        [(k, *v) for k, v in names.items()])

    con.execute(
        f"""
        COPY (
          -- Dwell per stop comes from the events table's dw rows, not the raw
          -- door export: the historical months carry no stop_id at all, and
          -- dw rows are the same cycles after location-based re-attribution
          -- (delay_events), so they attribute correctly at bays/stations where
          -- the AVL stamp lags. dur_s is the dwell BLOB (door cycle ∪ any
          -- overlapping slow events), matching the dashboard's dwell metric;
          -- a cycle straddling a segment boundary contributes one row a side.
          WITH door AS (
            SELECT stop_id::VARCHAR AS stop_id,
                   count(*) AS n_door,
                   round(sum(dur_s), 1) AS door_s_total,
                   round(quantile_cont(dur_s, 0.5), 1) AS door_s_p50,
                   round(quantile_cont(dur_s, 0.9), 1) AS door_s_p90
            FROM read_parquet('{ev_glob}')
            WHERE cls = 'dw' AND stop_id IS NOT NULL
            GROUP BY 1
          ),
          pre AS (
            SELECT stop_id, count(*) AS n_pre,
                   round(sum(dur_s), 1) AS pre_s_total,
                   round(quantile_cont(dur_s, 0.5), 1) AS pre_s_p50,
                   round(quantile_cont(dur_s, 0.9), 1) AS pre_s_p90
            FROM read_parquet('{ev_glob}')
            WHERE cls = 'pre' AND stop_id IS NOT NULL GROUP BY 1
          ),
          post AS (
            SELECT stop_id, count(*) AS n_post,
                   round(sum(dur_s), 1) AS post_s_total,
                   round(quantile_cont(dur_s, 0.5), 1) AS post_s_p50,
                   round(quantile_cont(dur_s, 0.9), 1) AS post_s_p90
            FROM read_parquet('{ev_glob}')
            WHERE cls IN ('post', 'post2') AND stop_id IS NOT NULL GROUP BY 1
          )
          SELECT door.stop_id, names.stop_name,
                 names.signal_side, names.signal_dist_ft,
                 n_door, door_s_total, door_s_p50, door_s_p90,
                 coalesce(n_pre, 0) AS n_pre, pre_s_total, pre_s_p50, pre_s_p90,
                 coalesce(n_post, 0) AS n_post, post_s_total, post_s_p50, post_s_p90
          FROM door
          LEFT JOIN pre USING (stop_id)
          LEFT JOIN post USING (stop_id)
          LEFT JOIN names ON names.stop_id = door.stop_id
          ORDER BY n_door DESC
        ) TO '{out}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """
    )
    r = con.execute(f"""
        SELECT count(*), sum(n_door), sum(n_pre), sum(n_post)
        FROM read_parquet('{out}')""").fetchone()
    print(f"wrote {out}: {r[0]:,} stops, {r[1]:,} door events, "
          f"{r[2]:,} pre pieces, {r[3]:,} post pieces")
    print(con.execute(f"""
        SELECT stop_id, stop_name, n_door, door_s_p50, door_s_p90,
               n_pre, pre_s_p90, n_post, post_s_p90
        FROM read_parquet('{out}') LIMIT 5""").df().to_string(index=False))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--city", default="cta")
    args = ap.parse_args()
    build(args.city)


if __name__ == "__main__":
    main()
