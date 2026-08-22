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

Derived annotations:

  * (2026-08-10) the naive v1 stop-bar estimate was removed entirely per
    user decision; ``sha`` (intersections sha prefix) stamps every file so
    the dashboard can detect stale-tab / fresh-data mismatches.

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
import os
import time
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


CLASSES = ["nd", "pre", "post", "post_ns", "post2", "post2_ns", "dw"]


def build(city_id: str, suffix: str = "") -> None:
    city = get_city(city_id)
    base = REPO / "outputs" / "network" / city.city_id
    registry = json.loads((base / "segment_registry.json").read_text())
    sha12 = registry["meta"]["intersections_sha256"][:12]

    # Raw ping density (ping_density.py; optional — "raw pings" dist tab)
    ping_path = base / "ping_density.parquet"
    ping_counts: dict[str, dict[int, int]] = {}
    ping_speed: dict[str, dict[int, float]] = {}
    speed_w: dict[str, dict[int, int]] = {}
    if ping_path.exists():
        import pyarrow.parquet as _pq
        _cols = _pq.ParquetFile(ping_path).schema.names
        has_v = "n_v" in _cols
        q = ("SELECT seg_id, bucket, n, n_v, sum_v" if has_v
             else "SELECT seg_id, bucket, n, 0, 0.0")
        for seg_id_, b_, n_, nv_, sv_ in duckdb.connect().execute(
                f"{q} FROM read_parquet('{ping_path}')").fetchall():
            ping_counts.setdefault(seg_id_, {})[int(b_)] = int(n_)
            if nv_ and nv_ >= 5:
                ping_speed.setdefault(seg_id_, {})[int(b_)] = float(sv_) / nv_
        speed_w = ping_counts
    # Trajectory crossing times (delay_events --traj-speed): bucket avg
    # speed = bucket_len / mean(crossing dt) — the L/avg-crossing-time
    # estimator. Preferred over the ping-speed bucket mean, which is biased
    # by stop-zone milestone pings (positions stamped at fixed points with
    # live speeds). Replaces the speed OVERLAY only; ping counts still feed
    # the raw-pings tab.
    ts_glob = base / "traj_speed"
    if ts_glob.exists():
        ping_speed, speed_w = {}, {}
        bucket_m = 10.0 / FT_PER_M
        seg_len_m = {s: r["len_m"] for s, r in registry["segments"].items()}
        # Accumulate (n, sum_dt) per bucket one year at a time on a BOUNDED
        # connection. Aggregating all 957 days at once on a default
        # connection drove the spill to 27.8 GB and filled the volume
        # (2026-08-18): both sums are additive, so chunking is exact.
        _spill = base / "duckdb_spill"
        _spill.mkdir(parents=True, exist_ok=True)
        tcon = duckdb.connect()
        tcon.execute(f"SET temp_directory='{_spill}'")
        tcon.execute(f"SET memory_limit='"
                     f"{os.environ.get('DIST_MEMORY_LIMIT', '8GB')}'")
        tcon.execute("SET preserve_insertion_order=false")
        tcon.execute("SET threads=3")
        acc: dict[tuple[str, int], list] = {}
        _years = sorted({p_.name.split("=")[1][:7]
                         for p_ in ts_glob.glob("service_date=*")})
        for _y in _years:
            for seg_id_, b_, n_, sdt_ in tcon.execute(f"""
                    SELECT seg_id, bucket, sum(n), sum(sum_dt)
                    FROM read_parquet(
                      '{ts_glob}/service_date={_y}-*/route=*.parquet')
                    GROUP BY 1, 2""").fetchall():
                a = acc.setdefault((seg_id_, int(b_)), [0, 0.0])
                a[0] += int(n_); a[1] += float(sdt_)
            print(f"  traj_speed {_y}: {len(acc):,} buckets", flush=True)
        tcon.close()
        for (seg_id_, b_), (n_, sdt_) in acc.items():
            if n_ < 5 or sdt_ <= 0:
                continue
            L = seg_len_m.get(seg_id_)
            if L is None:
                continue
            nb = int(np.ceil(L / bucket_m))
            blen = L - (nb - 1) * bucket_m if b_ == nb - 1 else bucket_m
            ping_speed.setdefault(seg_id_, {})[b_] = blen / (sdt_ / n_)
            speed_w.setdefault(seg_id_, {})[b_] = n_
    seg_index = {s: i for i, s in enumerate(sorted(registry["segments"]))}

    # CTA keeps the original flat location; other cities nest under their id
    # (mirrors the payload layout dashboard/data/network/<city>/).
    out_dir = (
        REPO / "dashboard" / "data" / "network" / f"dist{suffix}"
        if city.city_id == "cta"
        else REPO / "dashboard" / "data" / "network" / city.city_id / f"dist{suffix}"
    )
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    con = duckdb.connect()
    spill = base / "duckdb_spill"
    spill.mkdir(parents=True, exist_ok=True)
    con.execute(f"SET temp_directory='{spill}'")
    con.execute(f"SET memory_limit='{os.environ.get('DIST_MEMORY_LIMIT', '8GB')}'")
    con.execute("SET preserve_insertion_order=false")
    con.execute("SET threads=3")
    glob = str(base / f"events{suffix}" / "service_date=*" / "route=*.parquet")
    sums_glob = str(base / f"event_sums{suffix}"
                    / "service_date=*" / "route=*.parquet")

    # ---- turn movements (turn_movements.py; annotation only) -------------
    mv_path = base / "movements.json"
    movements = json.loads(mv_path.read_text()) if mv_path.exists() else {}
    # (month, stop) -> near-side, straight from the monthly registration.
    # The events table also carries a near_side column now, but only dates
    # regenerated since 2026-08-21 have it; deriving here means the split
    # works across all 957 days without rewriting 12 GB of events.
    con.execute("CREATE TABLE nstop(ym TEXT, stop_id TEXT)")
    ns_rows = []
    for mp in sorted((base / "monthly_stops").glob("*.json")):
        try:
            raw = json.loads(mp.read_text())
        except Exception:  # noqa: BLE001
            continue
        ym = mp.stem
        ns_rows += [(ym, str(st["id"])) for stops in raw.values() for st in stops
                    if st.get("signal_side") == "near_side"]
    if ns_rows:
        con.executemany("INSERT INTO nstop VALUES (?, ?)", ns_rows)
        con.execute("CREATE INDEX IF NOT EXISTS nstop_i ON nstop(ym, stop_id)")
    print(f"near-side stop-months: {len(ns_rows):,}")

    # Era-complete (shape, seg) -> movement: movements.json is keyed by
    # canonical shape_ids, so historical traversals would all read '?' and
    # lose their per-movement splits.
    from analysis.network.turn_movements import movement_rows
    con.execute("CREATE TABLE mv(seg_id TEXT, shape_id TEXT, m TEXT)")
    _mrows = movement_rows(city, registry)
    if _mrows:
        con.executemany("INSERT INTO mv VALUES (?, ?, ?)",
                        [(seg, sh, m) for sh, seg, m in _mrows])

    # ---- segment adjacency along each shape (ghost zones) ----------------
    # For neighbor N of target S on shape sh, an N-event at off_N sits at
    # off_N + (x_hi_S − x_hi_N) in S's downstream-signal frame: negative =
    # past S's light, > len = upstream of S's start.
    # Every era's shapes, for the same reason as movements above: ghost
    # zones join on shape_id, so canonical-only would blank them historically.
    _all_shapes = dict(registry["shapes"])
    _era_dir = base / "era_shapes"
    if _era_dir.is_dir():
        for _p in sorted(_era_dir.glob("*.json")):
            try:
                _all_shapes.update(json.loads(_p.read_text()))
            except Exception:  # noqa: BLE001
                continue
    adj_rows = []
    for sh, rec in _all_shapes.items():
        sb = sorted(rec["seg_bounds"], key=lambda r: r[1])
        for a, b in zip(sb, sb[1:]):
            adj_rows.append((sh, b[0], a[0], a[2] - b[2]))  # b is a's next
            adj_rows.append((sh, a[0], b[0], b[2] - a[2]))  # a is b's prev
    next_of: dict[str, set] = {}
    prev_of: dict[str, set] = {}
    for _sh, nb, tgt, _shift in adj_rows:
        # rows come in pairs; nb with larger x_end than tgt is tgt's NEXT
        pass
    for sh_, rec_ in _all_shapes.items():
        sb_ = sorted(rec_["seg_bounds"], key=lambda r: r[1])
        for a_, b_ in zip(sb_, sb_[1:]):
            next_of.setdefault(a_[0], set()).add(b_[0])
            prev_of.setdefault(b_[0], set()).add(a_[0])
    con.execute("CREATE TABLE adj(shape_id TEXT, nb_seg TEXT, tgt_seg TEXT, "
                "shift DOUBLE)")
    if adj_rows:
        con.executemany("INSERT INTO adj VALUES (?, ?, ?, ?)", adj_rows)
    con.execute("CREATE TABLE seglen(seg_id TEXT, len_m DOUBLE)")
    con.executemany("INSERT INTO seglen VALUES (?, ?)",
                    [(s, r["len_m"]) for s, r in registry["segments"].items()])

    trav_glob_all = str(base / "traversals" / "service_date=*" / "route=*.parquet")

    def yglob(g: str, y: str) -> str:
        """Restrict a service_date=* glob to one chunk (YYYY or YYYY-MM), so
        duckdb prunes files rather than reading 957 days and filtering rows."""
        return g.replace("service_date=*", f"service_date={y}-*")

    # ---- bucket aggregation (split by movement; '?' = unknown shape) -----
    # Shape per (seg, trip) via event_sums; min() collapses the handful of
    # cross-route trip_key collisions (~6/day) to one shape.
    def _main_rows(y: str):
        return con.execute(
            f"""
        WITH shp AS (
          SELECT seg_id, trip_key, min(shape_id) AS shape_id
          FROM read_parquet('{yglob(sums_glob, y)}') GROUP BY 1, 2
        )
        SELECT e.seg_id,
               CASE WHEN e.cls IN ('post','post2') AND (ns.stop_id IS NOT NULL)
                    THEN e.cls || '_ns' ELSE e.cls END AS fcls,
               floor(e.off_down_m * {FT_PER_M} / {BUCKET_FT})::INT AS bucket,
               coalesce(mv.m, '?') AS mvm,
               count(*) AS n,
               sum(e.dur_s) AS secs,
               sum(e.dur_s * coalesce(e.pax, 0)) AS pax_secs,
               count(*) FILTER (WHERE CASE WHEN e.cls = 'dw'
                                THEN e.is_last_all ELSE e.is_last END) AS n_last
        FROM read_parquet('{yglob(glob, y)}', union_by_name=true) e
        LEFT JOIN nstop ns ON ns.stop_id = e.stop_id
             AND ns.ym = strftime(e.service_date, '%Y%m')
        LEFT JOIN shp USING (seg_id, trip_key)
        LEFT JOIN mv ON mv.seg_id = e.seg_id AND mv.shape_id = shp.shape_id
        WHERE e.cls IN ('nd', 'pre', 'post', 'post2', 'dw')
        GROUP BY 1, 2, 3, 4
        """
        ).fetchall()

    # ---- ghost aggregation: neighbors' events in the ±10%-length zones ---
    def _ghost_rows(y: str):
        return con.execute(
            f"""
        WITH shp AS (
          SELECT seg_id, trip_key, min(shape_id) AS shape_id
          FROM read_parquet('{yglob(sums_glob, y)}') GROUP BY 1, 2
        )
        SELECT adj.tgt_seg,
               CASE WHEN e.cls IN ('post','post2') AND (ns.stop_id IS NOT NULL)
                    THEN e.cls || '_ns' ELSE e.cls END AS fcls,
               floor((e.off_down_m + adj.shift) * {FT_PER_M} / {BUCKET_FT})::INT AS bucket,
               coalesce(mv.m, '?') AS mvm,
               count(*) AS n,
               sum(e.dur_s) AS secs,
               sum(e.dur_s * coalesce(e.pax, 0)) AS pax_secs,
               count(*) FILTER (WHERE CASE WHEN e.cls = 'dw'
                                THEN e.is_last_all ELSE e.is_last END) AS n_last
        FROM read_parquet('{yglob(glob, y)}', union_by_name=true) e
        LEFT JOIN nstop ns ON ns.stop_id = e.stop_id
             AND ns.ym = strftime(e.service_date, '%Y%m')
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

    per_seg: dict[str, dict] = {}
    per_seg_mv: dict[str, dict[str, dict]] = {}
    per_seg_gh: dict[str, dict] = {}
    per_seg_mv_gh: dict[str, dict[str, dict]] = {}

    def _fold_main(rows):
      for seg_id, fcls, bucket, mvm, n, secs, pax_secs, n_last in rows:
        b = int(bucket)
        d = per_seg.setdefault(seg_id, {})
        d.setdefault(fcls, {})[b] = d.get(fcls, {}).get(b, 0) + int(n)
        sd = d.setdefault(fcls + "_s", {})
        sd[b] = round(sd.get(b, 0.0) + float(secs), 1)
        pd_ = d.setdefault(fcls + "_p", {})
        pd_[b] = round(pd_.get(b, 0.0) + float(pax_secs), 1)
        if n_last:
            qd = d.setdefault(fcls + "_q", {})
            qd[b] = qd.get(b, 0) + int(n_last)
        if mvm != "?":
            md = per_seg_mv.setdefault(seg_id, {}).setdefault(mvm, {})
            md.setdefault(fcls, {})[b] = int(n)
            md.setdefault(fcls + "_s", {})[b] = round(float(secs), 1)
            md.setdefault(fcls + "_p", {})[b] = round(float(pax_secs), 1)
            if n_last:
                md.setdefault(fcls + "_q", {})[b] = int(n_last)

    def _fold_ghost(rows):
      for seg_id, fcls, bucket, mvm, n, secs, pax_secs, n_last in rows:
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
            pd_ = t.setdefault(fcls + "_p", {})
            pd_[b] = round(pd_.get(b, 0.0) + float(pax_secs), 1)
            if n_last:
                qd = t.setdefault(fcls + "_q", {})
                qd[b] = qd.get(b, 0) + int(n_last)

    # Drive both aggregations one year at a time. Bucket counters are
    # additive, so folding chunk by chunk is exact — and it keeps peak memory
    # and spill bounded. Running all 957 days in one query drove duckdb's
    # temp dir to 9.7 GB and nearly filled the volume (2026-08-18).
    years = sorted({d.name.split("=")[1][:7]
                    for d in (base / f"events{suffix}").glob("service_date=*")})
    for y in years:
        t_y = time.time()
        _fold_main(_main_rows(y))
        _fold_ghost(_ghost_rows(y))
        print(f"  {y}: folded ({time.time() - t_y:.0f}s)", flush=True)

    # Event-covered dates, straight off the partition names — counting
    # DISTINCT service_date meant re-reading 12 GB of events.
    ev_dates = sorted({d.name.split("=")[1]
                       for d in (base / f"events{suffix}").glob("service_date=*")})
    dates = (len(ev_dates),)

    # Traversal counts per segment over the SAME service dates as the events —
    # the denominator that turns summed delay seconds into per-trip averages.
    # Chunked by month like the aggregations above: the canonical view joins
    # segmap across 10 GB of traversals, and running it whole spilled 37.8 GB
    # and filled the volume (2026-08-18). Counts are additive.
    ev_months = sorted({d[:7] for d in ev_dates})
    n_trips: dict[str, int] = {}
    mv_trips: dict[str, dict[str, int]] = {}
    for ym in ev_months:
        create_canonical_view(
            con, yglob(trav_glob_all, ym), registry, city, view_name="trav_m")
        for seg_id, n in con.execute(
                "SELECT seg_id, count(*) FROM trav_m GROUP BY 1").fetchall():
            n_trips[seg_id] = n_trips.get(seg_id, 0) + int(n)
        for seg_id, m, n in con.execute("""
                SELECT t.seg_id, mv.m, count(*) FROM trav_m t
                JOIN mv ON mv.seg_id = t.seg_id AND mv.shape_id = t.shape_id
                GROUP BY 1, 2""").fetchall():
            d_ = mv_trips.setdefault(seg_id, {})
            d_[m] = d_.get(m, 0) + int(n)

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
        for cls in [c + suf for c in CLASSES for suf in ("", "_s", "_q", "_p")]:
            arr = [0] * n_buckets
            for b, n in classes.get(cls, {}).items():
                if 0 <= b < n_buckets:
                    arr[b] = n
                    # dw is an annotation layer, not a delay event
                    if not cls.endswith(("_s", "_q", "_p")) and cls != "dw":
                        total += n
            payload[cls] = arr
        # no-door cities (e.g. MBTA) have all-zero pax: drop the _p arrays
        # so the dashboard hides the passenger-seconds tab entirely.
        if not any(v for c in CLASSES for v in payload.get(c + "_p", [])):
            for c in CLASSES:
                payload.pop(c + "_p", None)
        payload["n_events"] = total
        payload["n_trips"] = int(n_trips.get(seg_id, 0))
        # Ghost zones: neighbors' events remapped into this segment's frame,
        # ±10% of length past each end (rendered at 50% opacity).
        G = max(1, int(np.ceil(len_ft * 0.1 / BUCKET_FT)))

        def _ghost_arrays(src_dict):
            lo, hi = {}, {}
            for cls in [c + s for c in CLASSES for s in ("", "_s", "_q", "_p")]:
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

        # Avg-speed overlay ghosts: neighbors' per-bucket speeds remapped
        # into this segment's frame (same ±10% window, drawn at 50% op).
        if ping_speed:
            def _nb_speed(nbs, bucket_of):
                num = den = 0.0
                for nb in nbs:
                    v = ping_speed.get(nb, {}).get(bucket_of(nb))
                    if v is None:
                        continue
                    w = speed_w.get(nb, {}).get(bucket_of(nb), 1)
                    num += v * w
                    den += w
                return round(num / den, 2) if den else None
            nb_next = next_of.get(seg_id, ())
            nb_prev = prev_of.get(seg_id, ())
            nB_of = {nb: int(np.ceil(
                registry["segments"][nb]["len_m"] * FT_PER_M / BUCKET_FT))
                for nb in set(nb_next) | set(nb_prev)}
            vlo = [_nb_speed(nb_next, lambda nb, i=i: nB_of[nb] - 1 - i)
                   for i in range(G)]
            vhi = [_nb_speed(nb_prev, lambda nb, i=i: i) for i in range(G)]
            if any(v is not None for v in vlo + vhi):
                payload["ping_v_lo"] = vlo
                payload["ping_v_hi"] = vhi

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
                    for cls in [c + s for c in CLASSES for s in ("", "_s", "_q", "_p")]:
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
        pc = ping_counts.get(seg_id)
        if pc:
            parr = [0] * n_buckets
            for b, n in pc.items():
                if 0 <= b < n_buckets:
                    parr[b] = n
            payload["ping"] = parr
        ps = ping_speed.get(seg_id)
        if ps:
            varr = [None] * n_buckets
            for b, v in ps.items():
                if 0 <= b < n_buckets:
                    varr[b] = round(v, 2)   # m/s, >=5 speed pings per bucket
            payload["ping_v"] = varr
        payload["sha"] = sha12
        n_events_total += total
        (out_dir / f"{sid}.json").write_text(json.dumps(payload))
        n_files += 1

    print(f"wrote {n_files} segment distribution files "
          f"({n_events_total:,} classified events over {dates[0]} dates) "
          f"→ {out_dir}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--city", default="cta")
    ap.add_argument("--events-suffix", default="",
                    help="read events<suffix>/event_sums<suffix>, write "
                         "dist<suffix>/ (e.g. 3mph for the --mph 3 pass)")
    args = ap.parse_args()
    build(args.city, suffix=args.events_suffix)


if __name__ == "__main__":
    main()
