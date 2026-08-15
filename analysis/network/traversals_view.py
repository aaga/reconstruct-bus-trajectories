"""Shared duckdb view over the traversal parquet, canonical-segment keyed.

Since the 2026-07-29 regen, ``run_reconstruct`` emits traversals keyed
DIRECTLY by canonical seg_ids (its per-shape ``seg_bounds`` come from the
registry's canonical segmentation), so the view's segment map is the
identity over each shape's ``seg_bounds``. The GROUP BY merge remains for
the rare shape that crosses the same canonical segment twice (loop
routes): boundary times are shared at joints (t_exit of one part ==
t_enter of the next), so summing part t_obs values is exact.

(The registry still emits ``traversal_map`` — legacy seg → canonical — for
analyses over ARCHIVED pre-regen traversal outputs, which were keyed by
legacy ped-signal-boundary segments. This view no longer uses it.)

``create_canonical_view(con, glob, registry, city)`` registers:

    trav(seg_id, route_id, shape_id, direction, service_date, trip_key,
         t_enter_utc, t_exit_utc, t_obs_s, n_pings_in_seg, max_gap_in_seg_s,
         hour_local, period, flags)

with one row per (trip, canonical segment), keeping only trips that covered
EVERY constituent of the canonical span (per that trip's shape). period and
hour_local are re-derived from the merged t_enter in the city's timezone.
"""

from __future__ import annotations

import duckdb

from dataio.cities import CityConfig


def _period_case(city: CityConfig, hour_expr: str) -> str:
    whens = []
    fallback = None
    for name, lo, hi in city.periods:
        if lo <= hi:
            whens.append(f"WHEN {hour_expr} >= {lo} AND {hour_expr} < {hi} THEN '{name}'")
        else:  # wraps midnight — use as ELSE
            fallback = name
    if fallback is None:
        fallback = city.periods[-1][0]
    return "CASE " + " ".join(whens) + f" ELSE '{fallback}' END"


def create_canonical_view(
    con: "duckdb.DuckDBPyConnection",
    traversals_glob: str,
    registry: dict,
    city: CityConfig,
    *,
    view_name: str = "trav",
    door_sidecar_glob: str | None = None,
) -> None:
    """See module docstring. When ``door_sidecar_glob`` is given (and files
    exist), the view gains door/APC columns summed over constituents:
    has_door (every constituent covered), door_n, dwell_s, ons, offs,
    load_sum. Without a sidecar these come back as 0/NULL."""
    # Identity map over canonical seg_bounds: stored seg_id == canonical
    # seg_id since the 2026-07 regen. k = occurrences of the segment within
    # the shape (loop routes can cross one canonical segment twice; the
    # coverage filter then requires both parts present before merging).
    rows = []
    for shape_id, rec in registry["shapes"].items():
        k_by_seg: dict[str, int] = {}
        for seg_id, _, _ in rec["seg_bounds"]:
            k_by_seg[seg_id] = k_by_seg.get(seg_id, 0) + 1
        for seg_id, k in k_by_seg.items():
            rows.append((shape_id, seg_id, seg_id, k))
    con.execute(
        "CREATE OR REPLACE TABLE segmap(shape_id TEXT, old_seg TEXT, new_seg TEXT, k INT)"
    )
    con.executemany("INSERT INTO segmap VALUES (?, ?, ?, ?)", rows)

    # NB: for a TIMESTAMPTZ, a single AT TIME ZONE converts to local naive
    # time; chaining a second one re-interprets and lands back in UTC.
    import glob as _globmod

    have_doors = bool(door_sidecar_glob) and bool(_globmod.glob(door_sidecar_glob))
    if have_doors:
        door_join = (
            f"LEFT JOIN read_parquet('{door_sidecar_glob}') sc "
            "ON sc.trip_key = tr.trip_key AND sc.seg_id = tr.seg_id"
        )
        door_part_cols = (
            "(sc.trip_key IS NOT NULL) AS part_covered, sc.door_n, "
            "sc.dwell_s AS part_dwell_s, sc.ons, sc.offs, sc.load_sum, "
            "sc.load_in AS part_load_in,"
        )
        door_merge_cols = """
            (count(*) FILTER (WHERE part_covered) = count(*)) AS has_door,
            coalesce(sum(door_n), 0) AS door_n,
            coalesce(sum(part_dwell_s), 0) AS dwell_s,
            coalesce(sum(ons), 0) AS ons,
            coalesce(sum(offs), 0) AS offs,
            coalesce(sum(load_sum), 0) AS load_sum,
            coalesce(arg_min(part_load_in, t_enter_utc), 0) AS load_in,"""
    else:
        door_join = ""
        door_part_cols = ""
        door_merge_cols = """
            FALSE AS has_door, 0 AS door_n, 0.0 AS dwell_s,
            0 AS ons, 0 AS offs, 0 AS load_sum, 0 AS load_in,"""

    hour_expr = f"hour(t_enter_utc AT TIME ZONE '{city.tz}')"
    con.execute(
        f"""
        CREATE OR REPLACE VIEW {view_name} AS
        WITH joined AS (
          SELECT m.new_seg, m.k, {door_part_cols} tr.*
          FROM read_parquet('{traversals_glob}') tr
          JOIN segmap m
            ON m.shape_id = tr.shape_id AND m.old_seg = tr.seg_id
          {door_join}
        ),
        merged AS (
          SELECT
            new_seg AS seg_id,
            any_value(route_id) AS route_id,
            shape_id,
            any_value(direction) AS direction,
            service_date,
            trip_key,
            min(t_enter_utc) AS t_enter_utc,
            max(t_exit_utc) AS t_exit_utc,
            sum(t_obs_s) AS t_obs_s,
            sum(n_pings_in_seg) AS n_pings_in_seg,
            max(max_gap_in_seg_s) AS max_gap_in_seg_s,
            max(flags) AS flags,
            {door_merge_cols}
            count(*) AS n_parts,
            any_value(k) AS k
          FROM joined
          GROUP BY new_seg, shape_id, service_date, trip_key
        )
        SELECT
          seg_id, route_id, shape_id, direction, service_date, trip_key,
          t_enter_utc, t_exit_utc, t_obs_s, n_pings_in_seg, max_gap_in_seg_s,
          {hour_expr}::UTINYINT AS hour_local,
          {_period_case(city, hour_expr)} AS period,
          flags, has_door, door_n, dwell_s, ons, offs, load_sum, load_in
        FROM merged
        WHERE n_parts = k        -- full coverage of the canonical span only
        """
    )
