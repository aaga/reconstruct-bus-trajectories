"""Command-line entry point."""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from .pipeline import reconstruct_csv
from .plot import plot_speed_profile, plot_time_space
from .serialize import save_records, save_records_npz, to_pchip_record
from .viz import make_interactive_html
from .viz_compare import make_comparison_html
from .way_match import (
    ValhallaUnreachable,
    build_cache as build_way_cache,
    save_cache as save_way_cache,
)
from .intersections import (
    OverpassUnreachable,
    build_intersections,
    save_intersections,
)


def _cmd_reconstruct(args: argparse.Namespace) -> int:
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    recons = reconstruct_csv(
        csv_path=args.csv,
        gtfs_zip_path=args.gtfs,
        route_id=args.route,
        pattern_id=args.pattern,
        matcher_name=args.matcher,
        max_perp_m=args.max_perp,
        bandwidth=args.bandwidth,
        degree=args.degree,
    )
    print(f"Reconstructed {len(recons)} trips on route {args.route} pattern {args.pattern}")

    for trip_id, r in recons.items():
        # per-trip CSV: t, d_raw, x_smooth, v, a (in SI then mph for convenience)
        ts = np.linspace(r.t[0], r.t[-1], len(r.t))
        f = r.smoothed.f
        df = pd.DataFrame(
            {
                "t_s": r.t,
                "d_raw_m": r.d,
                "x_smooth_m": f(r.t),
            }
        )
        df["v_mps"] = f.derivative()(r.t)
        df["v_mph"] = df["v_mps"] * 2.23694
        df["a_mps2"] = f.derivative(2)(r.t)
        df["a_mph_per_s"] = df["a_mps2"] * 2.23694
        df.to_csv(out_dir / f"trip_{trip_id}.csv", index=False)
        if args.speed_plots:
            plot_speed_profile(r, out_dir / f"trip_{trip_id}_speed.png")

    plot_time_space(
        recons,
        out_dir / "time_space.png",
        title=f"Reconstructed trajectories — route {args.route} pattern {args.pattern}",
    )

    if args.html:
        html_path = make_interactive_html(
            recons,
            out_dir / "time_space.html",
            title=f"Reconstructed trajectories — route {args.route} pattern {args.pattern}",
            embed_js=args.embed_js,
        )
        print(f"Wrote interactive viewer: {html_path}")

    if args.serialize:
        records = [to_pchip_record(r) for r in recons.values()]
        save_records(records, out_dir / "trajectories.json")
        save_records_npz(records, out_dir / "trajectories.npz")
        print(
            f"Wrote compact trajectories: "
            f"{out_dir / 'trajectories.json'} and {out_dir / 'trajectories.npz'}"
        )

    # Summary table.
    print(
        f"{'trip_id':>10}  {'bus':>5}  {'pings':>5}  {'on_rt':>5}  "
        f"{'dur_min':>7}  {'len_mi':>6}  {'mean_v_mph':>10}"
    )
    for trip_id, r in recons.items():
        dur_min = (r.t[-1] - r.t[0]) / 60.0
        len_m = float(r.smoothed.f(r.t[-1]) - r.smoothed.f(r.t[0]))
        mean_v = (len_m / 1609.344) / (dur_min / 60.0) if dur_min > 0 else 0.0
        print(
            f"{trip_id:>10}  {r.meta.bus_id:>5}  {r.meta.n_pings:>5}  "
            f"{r.meta.n_on_route:>5}  {dur_min:>7.2f}  {len_m / 1609.344:>6.2f}  "
            f"{mean_v:>10.2f}"
        )
    return 0


def _cmd_compare(args: argparse.Namespace) -> int:
    bw_dirs = [Path(d) for d in args.dirs]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    raw_dir = Path(args.raw_dir) if args.raw_dir else None
    out_path = make_comparison_html(
        bw_dirs=bw_dirs,
        out_path=out,
        raw_dir=raw_dir,
        title=args.title,
        embed_js=args.embed_js,
        gtfs_zip_path=Path(args.gtfs) if args.gtfs else None,
        pattern_id=args.pattern,
        chart_height_px=args.height,
        exclude_bus_ids=tuple(args.exclude_bus or ()),
        x_compress=args.x_compress,
    )
    print(f"Wrote comparison HTML: {out_path}")
    return 0


def _cmd_build_way_cache(args: argparse.Namespace) -> int:
    shape_ids = None
    if args.shape_ids:
        shape_ids = [s.strip() for s in args.shape_ids.split(",") if s.strip()]
    try:
        cache = build_way_cache(
            gtfs_zip_path=args.gtfs,
            endpoint=args.endpoint,
            shape_ids=shape_ids,
            costing=args.costing,
            timeout_s=args.timeout,
            progress=True,
        )
    except ValhallaUnreachable as e:
        print(f"Error: {e}")
        return 2

    out_path = save_way_cache(cache, args.out)

    n_total = len(cache)
    n_ok = sum(1 for segs in cache.values() if segs)
    n_err = n_total - n_ok
    print(f"Built cache for {n_ok}/{n_total} shapes; {n_err} empty/error.")
    if n_err:
        print("Empty shapes:")
        for sid, segs in cache.items():
            if not segs:
                print(f"  {sid}")
    print(f"Saved: {out_path}")
    return 0


def _cmd_build_intersections(args: argparse.Namespace) -> int:
    shape_ids = None
    if args.shape_ids:
        shape_ids = [s.strip() for s in args.shape_ids.split(",") if s.strip()]
    keep_types = tuple(t.strip() for t in args.keep_types.split(",") if t.strip())
    try:
        cache = build_intersections(
            way_cache_path=args.way_cache,
            gtfs_zip_path=args.gtfs,
            shape_ids=shape_ids,
            overpass_endpoint=args.overpass_endpoint,
            perp_threshold_m=args.perp_threshold,
            stop_sign_proximity_m=args.stop_proximity,
            keep_types=keep_types,
            cluster_gap_m=args.cluster_gap,
            progress=True,
        )
    except OverpassUnreachable as e:
        print(f"Error: {e}")
        return 2

    out_path = save_intersections(cache, args.out)
    n_total = sum(len(v) for v in cache.values())
    print(f"Saved {n_total} ControlPoint(s) across {len(cache)} shape(s): {out_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bus-trajectories")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("reconstruct", help="Reconstruct smooth trajectories")
    p.add_argument("csv", help="AVL archive CSV")
    p.add_argument("--gtfs", required=True, help="CTA GTFS zip")
    p.add_argument("--route", required=True, help="route_id, e.g. 22")
    p.add_argument("--pattern", required=True, help="pattern_id, e.g. 3936")
    p.add_argument("--matcher", default="shape_snap", choices=["shape_snap", "valhalla"])
    p.add_argument("--max-perp", type=float, default=50.0, dest="max_perp",
                   help="Off-route threshold in meters")
    p.add_argument("--bandwidth", type=int, default=20)
    p.add_argument("--degree", type=int, default=3)
    p.add_argument("--out", default="out/", help="Output directory")
    p.add_argument("--speed-plots", action="store_true",
                   help="Also emit per-trip speed profile PNGs")
    p.add_argument("--html", action="store_true",
                   help="Emit an interactive Plotly HTML viewer")
    p.add_argument("--embed-js", action="store_true",
                   help="Embed plotly.js in the HTML (offline-capable, larger file)")
    p.add_argument("--serialize", action="store_true",
                   help="Emit compact PCHIP records (trajectories.json + .npz)")
    p.set_defaults(func=_cmd_reconstruct)

    c = sub.add_parser(
        "compare",
        help="Build a multi-bandwidth comparison HTML viewer from existing runs",
    )
    c.add_argument(
        "dirs",
        nargs="+",
        help="One or more out_bw<N>/ directories produced by `reconstruct --serialize`",
    )
    c.add_argument(
        "--raw-dir",
        default=None,
        help="Directory to read raw per-trip CSVs from (defaults to the first dir)",
    )
    c.add_argument("--out", default="compare.html", help="Output HTML path")
    c.add_argument("--title", default="Bandwidth comparison")
    c.add_argument(
        "--embed-js",
        action="store_true",
        help="Embed plotly.js inline (offline-capable, larger file)",
    )
    c.add_argument(
        "--gtfs",
        default=None,
        help="GTFS zip — when given with --pattern, draws bus stops as horizontal lines",
    )
    c.add_argument(
        "--pattern",
        default=None,
        help="CTA pattern_id (used with --gtfs to find the right shape's stops)",
    )
    c.add_argument(
        "--height",
        type=int,
        default=1400,
        help="Chart height in pixels (taller = more x-compression)",
    )
    c.add_argument(
        "--exclude-bus",
        action="append",
        default=None,
        help="bus_id to exclude (repeatable)",
    )
    c.add_argument(
        "--x-compress",
        type=float,
        default=1.5,
        help="Visible x range = x_compress × data span; lower = wider/zoomier",
    )
    c.set_defaults(func=_cmd_compare)

    w = sub.add_parser(
        "build-way-cache",
        help=(
            "Map-match each GTFS bus shape onto OSM via Valhalla and produce "
            "a per-shape way-sequence cache (way_id, dist_along_route, direction)."
        ),
    )
    w.add_argument("--gtfs", required=True, help="GTFS zip")
    w.add_argument(
        "--endpoint",
        default="http://localhost:8002",
        help="Valhalla service base URL (default: http://localhost:8002)",
    )
    w.add_argument("--out", default="way_cache.json", help="Output JSON path")
    w.add_argument(
        "--shape-ids",
        default=None,
        dest="shape_ids",
        help="Comma-separated shape_ids to process (default: all bus shapes)",
    )
    w.add_argument("--costing", default="bus", help="Valhalla costing model (default: bus)")
    w.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="Per-shape Valhalla HTTP timeout in seconds",
    )
    w.set_defaults(func=_cmd_build_way_cache)

    i = sub.add_parser(
        "build-intersections",
        help=(
            "Enrich a way-match cache with controlled intersections (signal, "
            "stop, give_way) along each shape's route, via the Overpass API."
        ),
    )
    i.add_argument("--way-cache", required=True, dest="way_cache",
                   help="Path to way_cache.json from `build-way-cache`")
    i.add_argument("--gtfs", required=True, help="GTFS zip")
    i.add_argument("--out", default="intersections.json", help="Output JSON path")
    i.add_argument(
        "--shape-ids",
        default=None,
        dest="shape_ids",
        help="Comma-separated shape_ids (default: every shape in the way-cache)",
    )
    i.add_argument(
        "--overpass-endpoint",
        dest="overpass_endpoint",
        default="https://overpass-api.de/api/interpreter",
        help="Overpass API endpoint",
    )
    i.add_argument(
        "--perp-threshold",
        type=float,
        default=30.0,
        dest="perp_threshold",
        help="Max perpendicular distance (m) to consider an OSM node 'on the route'",
    )
    i.add_argument(
        "--stop-proximity",
        type=float,
        default=30.0,
        dest="stop_proximity",
        help="Max distance (m) upstream of an intersection to look for a stop/yield sign",
    )
    i.add_argument(
        "--keep-types",
        default="traffic_signals,stop",
        dest="keep_types",
        help="Comma-separated control types to keep (default: traffic_signals,stop). "
             "Use 'traffic_signals,stop,give_way' to include yields.",
    )
    i.add_argument(
        "--cluster-gap",
        type=float,
        default=0.015 * 1609.344,
        dest="cluster_gap",
        help="Merge consecutive same-type intersections within this distance (m). "
             "0 disables clustering. Default 0.015 mi (~24 m).",
    )
    i.set_defaults(func=_cmd_build_intersections)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
