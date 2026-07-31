"""Per-segment delay-location distributions → per-segment payload files.

Buckets classified delay-event locations (see delay_events.py) into 10 ft
bins of distance-upstream-from-the-downstream-signal, per class:

    nd    non-dwell events                       (red)
    y     non-dwell events queued for a bus stop (dark yellow; see below)
    pre   pre-boarding portion                   (turquoise)
    post  post-boarding, single door cycle       (purple)
    post2 post-boarding with swallowed cycles    (slashed purple)

Derived annotations (2026-07-31):

  * dwell clusters — dw rows (door∪event blobs) gap-clustered per segment
    (split at >50 ft gaps); each cluster's median off marks where buses
    ACTUALLY stop, flagged near_stop when within 75 ft of a pole-projected
    stop. Rendered as blue vertical lines (solid near a stop, else dotted).
  * yellow reclassification — an nd piece turns 'y' when its midpoint sits
    within 50 ft of a near-stop cluster median AND the trip's next stopped
    piece (among nd/dw, ≤60 s later) is a dw blob inside that cluster:
    the bus was queued for the stop, not the signal.
  * stop bar estimate — p15 of last-piece ('last stop') nd offsets
    (yellow excluded), n ≥ 30. Naive v1.
  * back of queue estimate — p85 of each traversal's FIRST nd piece offset
    (yellow excluded), n ≥ 30. Naive v1.

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
YELLOW_NEAR_M = 50.0 / FT_PER_M     # nd midpoint → near-stop cluster median
YELLOW_MAX_GAP_S = 60.0             # nd end → dw start ("immediately")
MIN_N_ESTIMATES = 30                # support for stop-bar / queue-back

CLASSES = ["nd", "y", "pre", "post", "post2"]

# The yellow condition, shared by the reclassification pass (positive) and
# the estimator pass (negated) — one definition, no drift.
_YELLOW_COND = f"""EXISTS (
      SELECT 1 FROM clus c
      WHERE c.seg_id = s.seg_id AND c.near_stop
        AND abs(s.off_down_m - c.med_m) <= {YELLOW_NEAR_M}
        AND s.nxt_cls = 'dw'
        AND s.nxt_ts - s.t_end_s <= {YELLOW_MAX_GAP_S}
        AND s.nxt_off BETWEEN c.lo_m - 7.62 AND c.hi_m + 7.62
    )"""

_SEQD = """SELECT seg_id, trip_key, cls, off_down_m, dur_s, is_last,
             t_start_s, t_end_s,
             LEAD(cls)        OVER w AS nxt_cls,
             LEAD(t_start_s)  OVER w AS nxt_ts,
             LEAD(off_down_m) OVER w AS nxt_off
      FROM read_parquet('{glob}')
      WHERE cls IN ('nd', 'dw')
      WINDOW w AS (PARTITION BY trip_key, seg_id ORDER BY t_start_s)"""


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
    seqd = _SEQD.format(glob=glob)

    # ---- dwell clusters --------------------------------------------------
    clusters = _dwell_clusters(con, glob, registry)
    con.execute(
        "CREATE TABLE clus(seg_id TEXT, lo_m DOUBLE, hi_m DOUBLE, "
        "med_m DOUBLE, n INT, near_stop BOOLEAN)"
    )
    if clusters:
        con.executemany("INSERT INTO clus VALUES (?, ?, ?, ?, ?, ?)", clusters)

    # ---- yellow reclassification + bucket aggregation --------------------
    rows = con.execute(
        f"""
        WITH seqd AS ({seqd}),
        ndf AS (
          SELECT s.*, {_YELLOW_COND} AS is_yellow
          FROM seqd s WHERE s.cls = 'nd'
        ),
        pieces AS (
          SELECT seg_id,
                 CASE WHEN is_yellow THEN 'y' ELSE 'nd' END AS fcls,
                 off_down_m, dur_s, is_last
          FROM ndf
          UNION ALL
          SELECT seg_id, cls AS fcls, off_down_m, dur_s, is_last
          FROM read_parquet('{glob}') WHERE cls IN ('pre', 'post', 'post2')
        )
        SELECT seg_id, fcls,
               floor(off_down_m * {FT_PER_M} / {BUCKET_FT})::INT AS bucket,
               count(*) AS n,
               sum(dur_s) AS secs,
               count(*) FILTER (WHERE is_last) AS n_last
        FROM pieces GROUP BY 1, 2, 3
        """
    ).fetchall()

    # ---- stop bar / back of queue (naive v1) -----------------------------
    est = {
        r[0]: (r[1], r[2])
        for r in con.execute(
            f"""
            WITH seqd AS ({seqd}),
            ndpure AS (
              SELECT s.* FROM seqd s
              WHERE s.cls = 'nd' AND NOT {_YELLOW_COND}
            ),
            firsts AS (
              SELECT seg_id, trip_key,
                     arg_min(off_down_m, t_start_s) AS first_off
              FROM ndpure GROUP BY 1, 2
            ),
            bar AS (
              SELECT seg_id, quantile_cont(off_down_m, 0.15) AS bar_m
              FROM ndpure WHERE is_last GROUP BY 1
              HAVING count(*) >= {MIN_N_ESTIMATES}
            ),
            qb AS (
              SELECT seg_id, quantile_cont(first_off, 0.85) AS qb_m
              FROM firsts GROUP BY 1
              HAVING count(*) >= {MIN_N_ESTIMATES}
            )
            SELECT coalesce(bar.seg_id, qb.seg_id), bar.bar_m, qb.qb_m
            FROM bar FULL OUTER JOIN qb ON bar.seg_id = qb.seg_id
            """
        ).fetchall()
    }

    per_seg: dict[str, dict] = {}
    for seg_id, fcls, bucket, n, secs, n_last in rows:
        d = per_seg.setdefault(seg_id, {})
        d.setdefault(fcls, {})[int(bucket)] = int(n)
        d.setdefault(fcls + "_s", {})[int(bucket)] = round(float(secs), 1)
        if n_last:
            d.setdefault(fcls + "_q", {})[int(bucket)] = int(n_last)

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
                    if not cls.endswith(("_s", "_q")):
                        total += n
            payload[cls] = arr
        payload["n_events"] = total
        payload["n_trips"] = int(n_trips.get(seg_id, 0))
        payload["dwell_clusters"] = clus_by_seg.get(seg_id, [])
        bar_qb = est.get(seg_id, (None, None))
        payload["stopbar_ft"] = (
            round(bar_qb[0] * FT_PER_M, 1) if bar_qb[0] is not None else None
        )
        payload["queueback_ft"] = (
            round(bar_qb[1] * FT_PER_M, 1) if bar_qb[1] is not None else None
        )
        n_events_total += total
        (out_dir / f"{sid}.json").write_text(json.dumps(payload))
        n_files += 1

    n_bar = sum(1 for v in est.values() if v[0] is not None)
    n_qb = sum(1 for v in est.values() if v[1] is not None)
    print(f"wrote {n_files} segment distribution files "
          f"({n_events_total:,} classified events over {dates[0]} dates; "
          f"{len(clusters):,} dwell clusters; stop-bar est on {n_bar:,} segs, "
          f"queue-back on {n_qb:,}) → {out_dir}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--city", default="cta")
    args = ap.parse_args()
    build(args.city)


if __name__ == "__main__":
    main()
