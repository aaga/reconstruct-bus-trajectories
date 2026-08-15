"""Raw AVL ping density per segment, in the distribution view's 10 ft
buckets — BEFORE any trajectory reconstruction.

Every archive ping of every trip that the traversal batch assigned to a
shape is projected onto that shape (nearest densified vertex, 1.5 m
spacing, 60 m snap cap), located in the shape's per-segment bounds, and
counted into 10 ft buckets measured upstream of the downstream signal —
the exact frame the distribution view plots.

Output: outputs/network/<city>/ping_density.parquet
    (seg_id TEXT, bucket INT, n BIGINT)

Consumed by build_distributions ("ping" array per dist file).

Usage:
    PYTHONPATH=src uv run python analysis/network/ping_density.py --city cta
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

import duckdb
import numpy as np

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from dataio.cities import get_city  # noqa: E402
from dataio.gtfs import load_gtfs_shape_with_dist  # noqa: E402

BUCKET_FT = 10.0
FT_PER_M = 3.28084
DENSIFY_M = 1.5
SNAP_MAX_M = 60.0


def build(city_id: str) -> None:
    from scipy.spatial import cKDTree

    city = get_city(city_id)
    base = REPO / "outputs" / "network" / city.city_id
    registry = json.loads((base / "segment_registry.json").read_text())
    gtfs = city.resolve(city.gtfs_zip)
    cache = city.resolve(city.archive_cache_dir)
    glob = str(cache / f"agency={city.r2_agency}__*.parquet")
    trav_glob = str(base / "traversals" / "service_date=*" / "*.parquet")
    tz = city.tz

    con = duckdb.connect()
    con.execute(f"SET temp_directory='{base / 'duckdb_spill'}'")

    # trip -> shape from the traversal batch (geometric assignment)
    t0 = time.time()
    inter = base / "ping_density_intermediate.parquet"
    con.execute(f"""
        COPY (
          WITH tk AS (
            SELECT DISTINCT trip_key, shape_id
            FROM read_parquet('{trav_glob}')
          )
          SELECT tk.shape_id, p.latitude AS lat, p.longitude AS lon
          FROM read_parquet('{glob}') p
          JOIN tk ON tk.trip_key =
            p.trip_id || '_' || p.vehicle_id || '_' ||
            strftime(CAST(p.timestamp AT TIME ZONE '{tz}' AS DATE), '%Y-%m-%d')
          WHERE p.latitude IS NOT NULL
        ) TO '{inter}' (FORMAT PARQUET)
    """)
    n_total = con.execute(
        f"SELECT count(*) FROM read_parquet('{inter}')").fetchone()[0]
    print(f"matched {n_total:,} pings to shapes "
          f"({time.time() - t0:.0f}s)", flush=True)

    shapes = registry["shapes"]
    counts: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    n_snapped = 0
    t0 = time.time()
    shape_ids = [r[0] for r in con.execute(
        f"SELECT DISTINCT shape_id FROM read_parquet('{inter}')").fetchall()]
    for si, sh in enumerate(shape_ids):
        rec = shapes.get(sh)
        if rec is None:
            continue
        poly, dist = load_gtfs_shape_with_dist(gtfs, sh)
        arr = np.asarray(poly, dtype=float)
        if dist is None:
            from core.mapmatch.shape_snap import equirect_cumulative_m
            cum = equirect_cumulative_m(arr)
        else:
            cum = np.asarray(dist, dtype=float)
        # densify to ~DENSIFY_M spacing in a local-meter frame
        mlat = 111320.0 * np.cos(np.radians(arr[:, 0].mean()))
        xy = np.column_stack([arr[:, 1] * mlat, arr[:, 0] * 111320.0])
        seglen = np.hypot(*np.diff(xy, axis=0).T)
        pts, ds = [], []
        for i in range(len(xy) - 1):
            n = max(1, int(seglen[i] // DENSIFY_M))
            t = np.linspace(0, 1, n, endpoint=False)
            pts.append(xy[i] + t[:, None] * (xy[i + 1] - xy[i]))
            ds.append(cum[i] + t * (cum[i + 1] - cum[i]))
        pts.append(xy[-1:]); ds.append(cum[-1:])
        pts = np.concatenate(pts); ds = np.concatenate(ds)
        tree = cKDTree(pts)

        pings = con.execute(
            "SELECT lat, lon FROM read_parquet(?) WHERE shape_id = ?",
            [str(inter), sh]).fetch_df()
        q = np.column_stack([pings["lon"].to_numpy() * mlat,
                             pings["lat"].to_numpy() * 111320.0])
        dd, ii = tree.query(q, distance_upper_bound=SNAP_MAX_M)
        ok = np.isfinite(dd)
        d_along = ds[np.clip(ii[ok], 0, len(ds) - 1)]
        n_snapped += int(ok.sum())

        # locate in this shape's segment bounds; bucket from x_end
        for seg_id, x0, x1 in rec["seg_bounds"]:
            m = (d_along > x0) & (d_along <= x1)
            if not m.any():
                continue
            b = np.floor((x1 - d_along[m]) * FT_PER_M / BUCKET_FT).astype(int)
            for bb, cnt in zip(*np.unique(b, return_counts=True)):
                counts[seg_id][int(bb)] += int(cnt)
        if (si + 1) % 100 == 0:
            print(f"  [{si + 1}/{len(shape_ids)}] shapes "
                  f"({time.time() - t0:.0f}s)", flush=True)

    rows = [(s, b, n) for s, d_ in counts.items() for b, n in d_.items()]
    out = base / "ping_density.parquet"
    con.execute("CREATE TABLE pd(seg_id TEXT, bucket INT, n BIGINT)")
    con.executemany("INSERT INTO pd VALUES (?, ?, ?)", rows)
    con.execute(f"COPY pd TO '{out}' (FORMAT PARQUET)")
    inter.unlink()
    print(f"wrote {out}: {len(counts):,} segments, {len(rows):,} buckets, "
          f"{n_snapped:,}/{n_total:,} pings snapped "
          f"({time.time() - t0:.0f}s)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--city", default="cta")
    args = ap.parse_args()
    build(args.city)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
