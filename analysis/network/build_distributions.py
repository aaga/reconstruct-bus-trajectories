"""Per-segment delay-location distributions → per-segment payload files.

Buckets classified delay-event locations (see delay_events.py) into 10 ft
bins of distance-upstream-from-the-downstream-signal, per class:

    nd    non-dwell events                       (red)
    pre   pre-boarding portion                   (turquoise)
    post  post-boarding, single door cycle       (purple)
    post2 post-boarding with swallowed cycles    (slashed purple)
    dw    door-event blobs (2026-08-05)          (blue; hidden behind the
          "door events" checkbox, excluded from n_events; dw_q counts use
          is_last_all — the dw-inclusive last-piece flag)

Derived annotations (2026-07-31):

  * dwell clusters — dw rows (door∪event blobs) gap-clustered per segment
    (split at >50 ft gaps); each cluster's median off marks where buses
    ACTUALLY stop, flagged near_stop when within 75 ft of a pole-projected
    stop. Rendered as blue vertical lines (solid near a stop, else dotted).
  * stop bar estimate — p15 of last-piece ('last stop') nd offsets,
    n ≥ 30. Naive v1.

(2026-08-04: the yellow "queued for stop" reclassification was removed —
under the event definitions almost no queued-for-stop delay survives as a
separate nd piece, so it never worked as intended.)

One small JSON per segment (fetched on click):
    dashboard/data/network/[<city>/]dist/<sid>.json

All-data (no filters) — v1 per user decision.

Usage:
    PYTHONPATH=src uv run python analysis/network/build_distributions.py --city cta
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

import duckdb
import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

from analysis.network.traversals_view import create_canonical_view  # noqa: E402
from dataio.cities import get_city  # noqa: E402

BUCKET_FT = 10.0
FT_PER_M = 3.28084

CLUSTER_GAP_M = 50.0 / FT_PER_M     # dw gap that splits clusters
NEAR_STOP_M = 75.0 / FT_PER_M       # cluster median → pole-projected stop
MIN_N_ESTIMATES = 30                # support for stop-bar

CLASSES = ["nd", "pre", "post", "post2", "dw"]


def dwell_clusters_for_bins(
    bins: list[tuple[int, int]], stop_offs: list[float]
) -> list[tuple[int, int, float, int, bool]]:
    """Gap-cluster 1 m-binned dw offsets → (lo, hi, median, n, near_stop).

    Pure helper (unit-tested): ``bins`` are ascending (meter, count).
    """
    out = []
    cluster: list[tuple[int, int]] = []
    for m, n in bins + [(None, None)]:
        if m is not None and (not cluster or m - cluster[-1][0] <= CLUSTER_GAP_M):
            cluster.append((m, n))
            continue
        if cluster:
            tot = sum(c for _, c in cluster)
            cum, med = 0, cluster[0][0]
            for mm, nn in cluster:
                cum += nn
                if cum >= tot / 2:
                    med = mm
                    break
            near = any(abs(med - so) <= NEAR_STOP_M for so in stop_offs)
            out.append((cluster[0][0], cluster[-1][0], float(med), int(tot), near))
        cluster = [(m, n)] if m is not None else []
    return out


def _dwell_clusters(con, glob: str, registry: dict) -> list[tuple]:
    rows = con.execute(
        f"""SELECT seg_id, round(off_down_m)::INT AS m, count(*) AS n
            FROM read_parquet('{glob}') WHERE cls = 'dw'
            GROUP BY 1, 2 ORDER BY 1, 2"""
    ).fetchall()
    segs = registry["segments"]
    by_seg: dict[str, list] = {}
    for seg_id, m, n in rows:
        by_seg.setdefault(seg_id, []).append((m, n))
    out: list[tuple] = []
    for seg_id, bins in by_seg.items():
        stop_offs = [s["off_m"] for s in segs.get(seg_id, {}).get("stops_off", [])]
        for lo, hi, med, n, near in dwell_clusters_for_bins(bins, stop_offs):
            out.append((seg_id, lo, hi, med, n, near))
    return out


def build(city_id: str) -> None:
    city = get_city(city_id)
    base = REPO / "outputs" / "network" / city.city_id
    registry = json.loads((base / "segment_registry.json").read_text())
    seg_index = {s: i for i, s in enumerate(sorted(registry["segments"]))}

    # CTA keeps the original flat location; other cities nest under their id
    # (mirrors the payload layout dashboard/data/network/<city>/).
    out_dir = (
        REPO / "dashboard" / "data" / "network" / "dist"
        if city.city_id == "cta"
        else REPO / "dashboard" / "data" / "network" / city.city_id / "dist"
    )
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    con = duckdb.connect()
    con.execute("SET threads=4")
    glob = str(base / "events" / "service_date=*" / "route=*.parquet")
    sums_glob = str(base / "event_sums" / "service_date=*" / "route=*.parquet")

    # ---- dwell clusters (blue-line annotation) ---------------------------
    clusters = _dwell_clusters(con, glob, registry)

    # ---- turn movements (turn_movements.py; annotation only) -------------
    mv_path = base / "movements.json"
    movements = json.loads(mv_path.read_text()) if mv_path.exists() else {}
    con.execute("CREATE TABLE mv(seg_id TEXT, shape_id TEXT, m TEXT)")
    if movements:
        con.executemany(
            "INSERT INTO mv VALUES (?, ?, ?)",
            [(s, sh, m) for s, d_ in movements.items() for sh, m in d_.items()],
        )

    # ---- segment adjacency along each shape (ghost zones) ----------------
    # For neighbor N of target S on shape sh, an N-event at off_N sits at
    # off_N + (x_hi_S − x_hi_N) in S's downstream-signal frame: negative =
    # past S's light, > len = upstream of S's start.
    adj_rows = []
    for sh, rec in registry["shapes"].items():
        sb = sorted(rec["seg_bounds"], key=lambda r: r[1])
        for a, b in zip(sb, sb[1:]):
            adj_rows.append((sh, b[0], a[0], a[2] - b[2]))  # b is a's next
            adj_rows.append((sh, a[0], b[0], b[2] - a[2]))  # a is b's prev
    con.execute("CREATE TABLE adj(shape_id TEXT, nb_seg TEXT, tgt_seg TEXT, "
                "shift DOUBLE)")
    if adj_rows:
        con.executemany("INSERT INTO adj VALUES (?, ?, ?, ?)", adj_rows)
    con.execute("CREATE TABLE seglen(seg_id TEXT, len_m DOUBLE)")
    con.executemany("INSERT INTO seglen VALUES (?, ?)",
                    [(s, r["len_m"]) for s, r in registry["segments"].items()])

    # ---- bucket aggregation (split by movement; '?' = unknown shape) -----
    # Shape per (seg, trip) via event_sums; min() collapses the handful of
    # cross-route trip_key collisions (~6/day) to one shape.
    rows = con.execute(
        f"""
        WITH shp AS (
          SELECT seg_id, trip_key, min(shape_id) AS shape_id
          FROM read_parquet('{sums_glob}') GROUP BY 1, 2
        )
        SELECT e.seg_id, e.cls AS fcls,
               floor(e.off_down_m * {FT_PER_M} / {BUCKET_FT})::INT AS bucket,
               coalesce(mv.m, '?') AS mvm,
               count(*) AS n,
               sum(e.dur_s) AS secs,
               count(*) FILTER (WHERE CASE WHEN e.cls = 'dw'
                                THEN e.is_last_all ELSE e.is_last END) AS n_last
        FROM read_parquet('{glob}') e
        LEFT JOIN shp USING (seg_id, trip_key)
        LEFT JOIN mv ON mv.seg_id = e.seg_id AND mv.shape_id = shp.shape_id
        WHERE e.cls IN ('nd', 'pre', 'post', 'post2', 'dw')
        GROUP BY 1, 2, 3, 4
        """
    ).fetchall()

    # ---- ghost aggregation: neighbors' events in the ±10%-length zones ---
    ghost_rows = con.execute(
        f"""
        WITH shp AS (
          SELECT seg_id, trip_key, min(shape_id) AS shape_id
          FROM read_parquet('{sums_glob}') GROUP BY 1, 2
        )
        SELECT adj.tgt_seg, e.cls AS fcls,
               floor((e.off_down_m + adj.shift) * {FT_PER_M} / {BUCKET_FT})::INT AS bucket,
               coalesce(mv.m, '?') AS mvm,
               count(*) AS n,
               sum(e.dur_s) AS secs,
               count(*) FILTER (WHERE CASE WHEN e.cls = 'dw'
                                THEN e.is_last_all ELSE e.is_last END) AS n_last
        FROM read_parquet('{glob}') e
        JOIN shp USING (seg_id, trip_key)
        JOIN adj ON adj.shape_id = shp.shape_id AND adj.nb_seg = e.seg_id
        JOIN seglen sl ON sl.seg_id = adj.tgt_seg
        LEFT JOIN mv ON mv.seg_id = adj.tgt_seg AND mv.shape_id = shp.shape_id
        WHERE e.cls IN ('nd', 'pre', 'post', 'post2', 'dw')
          AND ((e.off_down_m + adj.shift) BETWEEN -0.1 * sl.len_m AND -0.001
               OR (e.off_down_m + adj.shift) BETWEEN sl.len_m AND 1.1 * sl.len_m)
        GROUP BY 1, 2, 3, 4
        """
    ).fetchall()

    # ---- stop bar (naive v1) ---------------------------------------------
    est = {
        r[0]: r[1]
        for r in con.execute(
            f"""
            SELECT seg_id, quantile_cont(off_down_m, 0.15) AS bar_m
            FROM read_parquet('{glob}')
            WHERE cls = 'nd' AND is_last GROUP BY 1
            HAVING count(*) >= {MIN_N_ESTIMATES}
            """
        ).fetchall()
    }

    per_seg: dict[str, dict] = {}
    per_seg_mv: dict[str, dict[str, dict]] = {}
    for seg_id, fcls, bucket, mvm, n, secs, n_last in rows:
        b = int(bucket)
        d = per_seg.setdefault(seg_id, {})
        d.setdefault(fcls, {})[b] = d.get(fcls, {}).get(b, 0) + int(n)
        sd = d.setdefault(fcls + "_s", {})
        sd[b] = round(sd.get(b, 0.0) + float(secs), 1)
        if n_last:
            qd = d.setdefault(fcls + "_q", {})
            qd[b] = qd.get(b, 0) + int(n_last)
        if mvm != "?":
            md = per_seg_mv.setdefault(seg_id, {}).setdefault(mvm, {})
            md.setdefault(fcls, {})[b] = int(n)
            md.setdefault(fcls + "_s", {})[b] = round(float(secs), 1)
            if n_last:
                md.setdefault(fcls + "_q", {})[b] = int(n_last)

    per_seg_gh: dict[str, dict] = {}
    per_seg_mv_gh: dict[str, dict[str, dict]] = {}
    for seg_id, fcls, bucket, mvm, n, secs, n_last in ghost_rows:
        b = int(bucket)
        targets = [per_seg_gh.setdefault(seg_id, {})]
        if mvm != "?":
            targets.append(
                per_seg_mv_gh.setdefault(seg_id, {}).setdefault(mvm, {}))
        for t in targets:
            cd = t.setdefault(fcls, {})
            cd[b] = cd.get(b, 0) + int(n)
            sd = t.setdefault(fcls + "_s", {})
            sd[b] = round(sd.get(b, 0.0) + float(secs), 1)
            if n_last:
                qd = t.setdefault(fcls + "_q", {})
                qd[b] = qd.get(b, 0) + int(n_last)

    clus_by_seg: dict[str, list] = {}
    for seg_id, lo, hi, med, n, near in clusters:
        clus_by_seg.setdefault(seg_id, []).append(
            {"off_ft": round(med * FT_PER_M, 1), "n": n, "near_stop": near}
        )

    dates = con.execute(
        f"SELECT count(DISTINCT service_date) FROM read_parquet('{glob}')"
    ).fetchone()

    # Traversal counts per segment over the SAME service dates as the events —
    # the denominator that turns summed delay seconds into per-trip averages.
    trav_glob = str(base / "traversals" / "service_date=*" / "route=*.parquet")
    create_canonical_view(con, trav_glob, registry, city)
    n_trips = dict(con.execute(f"""
        SELECT seg_id, count(*) FROM trav
        WHERE service_date IN (
          SELECT DISTINCT service_date FROM read_parquet('{glob}'))
        GROUP BY 1
    """).fetchall())
    # traversal counts per (seg, movement) — the denominator for the
    # movement-filtered avg-seconds view
    mv_trips: dict[str, dict[str, int]] = {}
    for seg_id, m, n in con.execute(f"""
        SELECT t.seg_id, mv.m, count(*) FROM trav t
        JOIN mv ON mv.seg_id = t.seg_id AND mv.shape_id = t.shape_id
        WHERE t.service_date IN (
          SELECT DISTINCT service_date FROM read_parquet('{glob}'))
        GROUP BY 1, 2
    """).fetchall():
        mv_trips.setdefault(seg_id, {})[m] = int(n)

    n_files = 0
    n_events_total = 0
    for seg_id, classes in per_seg.items():
        sid = seg_index.get(seg_id)
        if sid is None:
            continue
        rec = registry["segments"][seg_id]
        len_ft = rec["len_m"] * FT_PER_M
        n_buckets = int(np.ceil(len_ft / BUCKET_FT))
        payload = {
            "v": 2,
            "bucket_ft": BUCKET_FT,
            "len_ft": round(len_ft, 1),
            "n_dates": dates[0],
        }
        total = 0
        for cls in [c + suf for c in CLASSES for suf in ("", "_s", "_q")]:
            arr = [0] * n_buckets
            for b, n in classes.get(cls, {}).items():
                if 0 <= b < n_buckets:
                    arr[b] = n
                    # dw is an annotation layer, not a delay event
                    if not cls.endswith(("_s", "_q")) and cls != "dw":
                        total += n
            payload[cls] = arr
        payload["n_events"] = total
        payload["n_trips"] = int(n_trips.get(seg_id, 0))
        payload["dwell_clusters"] = clus_by_seg.get(seg_id, [])
        # Ghost zones: neighbors' events remapped into this segment's frame,
        # ±10% of length past each end (rendered at 50% opacity).
        G = max(1, int(np.ceil(len_ft * 0.1 / BUCKET_FT)))

        def _ghost_arrays(src_dict):
            lo, hi = {}, {}
            for cls in [c + s for c in CLASSES for s in ("", "_s", "_q")]:
                alo = [0] * G
                ahi = [0] * G
                for b, v in src_dict.get(cls, {}).items():
                    if -G <= b < 0:
                        alo[b + G] = v
                    elif n_buckets <= b < n_buckets + G:
                        ahi[b - n_buckets] = v
                lo[cls] = alo
                hi[cls] = ahi
            return lo, hi

        gh = per_seg_gh.get(seg_id)
        if gh:
            payload["ghost_buckets"] = G
            payload["gh_lo"], payload["gh_hi"] = _ghost_arrays(gh)

        # Turn movements: label always (when known); per-movement array split
        # only for mixed segments — that's when the UI shows the filter.
        seg_mvs = sorted(set(movements.get(seg_id, {}).values()))
        if seg_mvs:
            payload["mvmt"] = {
                m: int(mv_trips.get(seg_id, {}).get(m, 0)) for m in seg_mvs}
            if len(seg_mvs) > 1:
                by = {}
                for m, mcls in per_seg_mv.get(seg_id, {}).items():
                    arrs = {}
                    for cls in [c + s for c in CLASSES for s in ("", "_s", "_q")]:
                        arr = [0] * n_buckets
                        for b, n in mcls.get(cls, {}).items():
                            if 0 <= b < n_buckets:
                                arr[b] = n
                        arrs[cls] = arr
                    mgh = per_seg_mv_gh.get(seg_id, {}).get(m)
                    if mgh:
                        arrs["gh_lo"], arrs["gh_hi"] = _ghost_arrays(mgh)
                    by[m] = arrs
                payload["by_mvmt"] = by
        bar_m = est.get(seg_id)
        payload["stopbar_ft"] = (
            round(bar_m * FT_PER_M, 1) if bar_m is not None else None
        )
        n_events_total += total
        (out_dir / f"{sid}.json").write_text(json.dumps(payload))
        n_files += 1

    n_bar = sum(1 for v in est.values() if v is not None)
    print(f"wrote {n_files} segment distribution files "
          f"({n_events_total:,} classified events over {dates[0]} dates; "
          f"{len(clusters):,} dwell clusters; stop-bar est on {n_bar:,} segs) "
          f"→ {out_dir}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--city", default="cta")
    args = ap.parse_args()
    build(args.city)


if __name__ == "__main__":
    main()
