"""Build the phone-vs-R2 trip comparison dashboard payloads.

For each observed (phone-app) trip:
  1. pull the webapp pings/events/meta from the Pages backup API,
  2. find the same trip in the public R2 AVL archive by (route, vehicle, trip_id),
  3. reconstruct BOTH trajectories on the same GTFS shape — phone @ bw=20,
     R2 @ bw=5 (the repo's CTA convention),
  4. attribute slowdown-window delays (v<5 mph events -> nearest stop/signal)
     on each reconstructed trajectory,
  5. emit one data/<key>.json on a shared UTC wall-clock axis (seconds since a
     per-trip global t0), consumed by the static dashboard in ./dashboard/.

Run:
  python observation_tool/analysis/build_comparison.py --token ridethebus
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import tempfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import csv as _csv
import zipfile

import numpy as np
import pandas as pd

import sys
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
from bus_trajectories.realtime import load_manifest, trip_avl_pings  # noqa: E402
from bus_trajectories.delay_decomposition.decompose import decompose_trip  # noqa: E402
from bus_trajectories.delay_decomposition.segments import build_segments_from_records  # noqa: E402
from bus_trajectories.intersections import (  # noqa: E402
    SIGNALIZED_CONTROL_TYPES, load_intersections,
)
from bus_trajectories.io import (  # noqa: E402
    load_avl_csv, load_gtfs_shape_with_dist, load_route_stops, shape_id_for_pattern,
)
from bus_trajectories.mapmatch import get_matcher  # noqa: E402
from bus_trajectories.pipeline import reconstruct_trip  # noqa: E402
from bus_trajectories.serialize import to_pchip_record  # noqa: E402

GTFS = REPO / "data" / "gtfs" / "cta_gtfs.zip"
INTERSECTIONS = REPO / "caches" / "cta" / "intersections.json"
PAGES = "https://cta-observation-tool.pages.dev/api/trips"
CHICAGO = ZoneInfo("America/Chicago")
MPS_TO_MPH = 2.23694
OUT = Path(__file__).resolve().parent / "dashboard" / "data"
AVL_DIR = Path(__file__).resolve().parent / "AVL data"  # CTA bus_state_history dumps

# Attribution reuses the pipeline's delay_decomposition stack unchanged, so an
# empty free-flow table is fine here — it only affects the (unused) d_congestion
# term, never the per-event attributions we harvest for the delay bars.
_NO_FREEFLOW: dict[str, float] = {}
# Minimum slowdown duration for a delay event (pipeline default is 15 s).
DELAY_MIN_DURATION_S = 10.0


# ----------------------------------------------------------------- fetching

def _pget(path: str, token: str) -> bytes:
    req = urllib.request.Request(
        f"{PAGES}/{path}?token={token}", headers={"User-Agent": "Mozilla/5.0 (analysis)"}
    )
    return urllib.request.urlopen(req, timeout=60).read()


def list_trips(token: str) -> list[dict]:
    return json.loads(_pget("", token))["trips"]


def good_trips(token: str, min_pings: int = 300, max_med_acc: float = 60.0) -> list[str]:
    """Keys of trips with dense, accurate phone GPS (worth comparing)."""
    keys = []
    for t in list_trips(token):
        rows = list(csv.DictReader(io.StringIO(_pget(f"{t['key']}/pings.csv", token).decode())))
        accs = sorted(float(r["accuracy_m"]) for r in rows if r.get("accuracy_m"))
        med = accs[len(accs) // 2] if accs else 1e9
        if len(rows) >= min_pings and med <= max_med_acc:
            keys.append(t["key"])
    return keys


# ----------------------------------------------------------------- helpers

def chicago_ms(local_str: str) -> int:
    """Local 'YYYY-MM-DD HH:MM:SS.ffffff' (Chicago wall time) -> UTC epoch ms.

    Accepts 3- or 6-digit fractional seconds (AVL dumps use .000)."""
    fmt = "%Y-%m-%d %H:%M:%S.%f" if "." in local_str else "%Y-%m-%d %H:%M:%S"
    dt = datetime.strptime(local_str, fmt).replace(tzinfo=CHICAGO)
    return int(dt.timestamp() * 1000)


# ----------------------------------------------------------------- AVL data

_AVL_CACHE = None
_EVENT_DESC = None


def event_descriptions() -> dict[int, str]:
    """{event_type id -> plain-text label} from bus_state_history_event_descriptions.csv."""
    global _EVENT_DESC
    if _EVENT_DESC is None:
        _EVENT_DESC = {}
        p = AVL_DIR / "bus_state_history_event_descriptions.csv"
        if p.exists():
            for row in _csv.DictReader(io.StringIO(p.read_text())):
                vals = list(row.values())
                try:
                    _EVENT_DESC[int(str(vals[0]).strip())] = str(vals[1]).strip()
                except (ValueError, IndexError):
                    continue
    return _EVENT_DESC


def load_avl() -> pd.DataFrame:
    """Concatenate every bus_state_hist_*.csv once (small dumps)."""
    global _AVL_CACHE
    if _AVL_CACHE is None:
        files = sorted(AVL_DIR.glob("bus_state_hist_*.csv"))
        frames = [pd.read_csv(f, dtype=str, keep_default_na=False) for f in files]
        _AVL_CACHE = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    return _AVL_CACHE


def _ival(v) -> int:
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return 0


def avl_bands(bus_id: str, route_id: str, t0_ms: int, lo_ms: int, hi_ms: int,
              stop_names: dict[str, str]) -> list[dict]:
    """Serviced stops (event_type 3) + any row with dwell>0 for this vehicle in
    the trip window, as door-open->door-close bars with passenger metrics.

    passenger_load is the load AFTER the stop; load-before is reconstructed
    from the door counts. on/off = front+rear boardings/alightings.
    """
    df = load_avl()
    if df.empty:
        return []
    desc = event_descriptions()
    sub = df[(df["bus_id"] == str(bus_id)) & (df["route_id"] == str(route_id))]
    bands = []
    for r in sub.itertuples(index=False):
        et = getattr(r, "event_time", "")
        if not et:
            continue
        try:
            open_ms = chicago_ms(et)
        except ValueError:
            continue
        if not (lo_ms <= open_ms <= hi_ms):
            continue
        etype = _ival(r.event_type)
        dwell = max(0, _ival(r.dwell_time))
        if not (etype == 3 or dwell > 0):
            continue
        on_f, on_r = _ival(r.fon), _ival(r.ron)
        off_f, off_r = _ival(r.foff), _ival(r.roff)
        ton, toff = on_f + on_r, off_f + off_r
        flow = ton + toff
        raw_load = str(getattr(r, "passenger_load", "")).strip()
        load_after = _ival(raw_load) if raw_load not in ("", "None") else None
        load_before = (load_after - (ton - toff)) if load_after is not None else None
        sid = str(r.stop_id)
        seq = str(r.stop_sequence)
        bands.append({
            "t_start": round((open_ms - t0_ms) / 1000, 1),
            "t_end": round((open_ms + dwell * 1000 - t0_ms) / 1000, 1),
            "category": "avl_stop" if etype == 3 else "avl_other",
            "event_type": etype,
            "event_desc": desc.get(etype, f"event {etype}"),
            "label": stop_names.get(sid, f"stop {sid}"),
            "stop_id": sid,
            "stop_seq": int(seq) if seq.isdigit() else None,
            "dwell_s": dwell,
            "load_before": load_before, "load_after": load_after,
            "on_total": ton, "off_total": toff, "flow": flow,
            "on_front": on_f, "on_rear": on_r, "off_front": off_f, "off_rear": off_r,
            "dwell_per_pax": round(dwell / flow, 1) if flow > 0 else None,
        })
    bands.sort(key=lambda b: b["t_start"])
    return bands


def anchor_ms(first_ping: pd.Timestamp, tz: ZoneInfo | timezone) -> int:
    return int(pd.Timestamp(first_ping).tz_localize(tz).timestamp() * 1000)


def route_shape_map(gtfs: Path) -> dict[str, set[str]]:
    """route_id -> set of bus shape_ids (from GTFS trips.txt), for shape search."""
    out: dict[str, set[str]] = {}
    with zipfile.ZipFile(gtfs) as z, z.open("trips.txt") as f:
        for r in _csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig")):
            sid = r.get("shape_id") or ""
            if sid.startswith("678") and len(sid) == 8:
                out.setdefault(str(r["route_id"]), set()).add(sid)
    return out


def choose_shape(df: pd.DataFrame, route: str, route_shapes: dict, hint_shape: str) -> str:
    """Pick the route's shape the pings actually follow, forward in time.

    The captured pattern can be the wrong direction (bus rode the reverse of
    that shape -> distances decrease -> monotone reconstruction collapses). We
    score every candidate shape by on-route fraction and net forward progress
    and keep the one with real forward motion.
    """
    cands = set(route_shapes.get(route, set())) | {hint_shape}
    lats, lons = df["latitude"].to_numpy(), df["longitude"].to_numpy()
    best, best_key = hint_shape, (-1.0, -1e18)
    for sid in sorted(cands):
        try:
            poly, dist = load_gtfs_shape_with_dist(GTFS, sid)
        except KeyError:
            continue
        kw = {"polyline_latlon": poly, "max_perp_m": 50.0}
        if dist is not None:
            kw["dist_along_m_per_vertex"] = dist
        res = get_matcher("shape_snap", **kw).match(lats, lons)
        on = res.on_route
        if on.sum() < 2:
            continue
        d = res.dist_along_m[on]
        frac, net = float(on.mean()), float(d[-1] - d[0])
        # Rank: forward-moving shapes by net progress; reverse/poor ones lose.
        key = (1.0 if (frac > 0.6 and net > 0) else 0.0, net if frac > 0.6 else frac)
        if key > best_key:
            best, best_key = sid, key
    return best


def ride_cluster(r2: pd.DataFrame, start_ms: int, end_ms: int,
                 pad_before_min: int = 90, pad_after_min: int = 30, gap_min: int = 30) -> pd.DataFrame:
    """Keep the contiguous time-run of R2 pings overlapping the ride window.

    ``trip_id`` is reused (weekly, and sometimes within a day across both
    directions), so a route+vehicle+trip_id filter over a wide window can pull
    a different physical trip. We clip to a window around the ride and keep the
    temporal cluster overlapping [start, end]. A generous pad keeps the bus's
    run from its terminal; a strict pad (0,0) isolates just the ride when the
    reused id otherwise contaminates the reconstruction."""
    if r2.empty:
        return r2
    lo, hi = start_ms - pad_before_min * 60_000, end_ms + pad_after_min * 60_000
    r2 = r2[(r2["epoch_ms"] >= lo) & (r2["epoch_ms"] <= hi)].sort_values("epoch_ms").reset_index(drop=True)
    if r2.empty:
        return r2
    gaps = r2["epoch_ms"].diff().gt(gap_min * 60_000).cumsum()
    best, best_overlap = r2, -1
    for _, grp in r2.groupby(gaps):
        ov = min(grp["epoch_ms"].max(), end_ms) - max(grp["epoch_ms"].min(), start_ms)
        if ov > best_overlap:
            best, best_overlap = grp, ov
    return best.reset_index(drop=True)


def _degenerate(recon) -> bool:
    """A reconstruction that barely moves forward / is mostly off-route — the
    sign that reused-trip_id pings of the wrong direction got mixed in."""
    if recon is None:
        return True
    net = float(recon.d[-1] - recon.d[0])
    frac = recon.meta.n_on_route / max(1, recon.meta.n_pings)
    return net < 500 or frac < 0.6


def matcher_for(shape_id: str):
    poly, dist = load_gtfs_shape_with_dist(GTFS, shape_id)
    kw = {"polyline_latlon": poly, "max_perp_m": 50.0}
    if dist is not None:
        kw["dist_along_m_per_vertex"] = dist
    return get_matcher("shape_snap", **kw)


def dense_grid(recon):
    """1 Hz (t, x, v_mph) grid over the on-route span for detection + plotting."""
    t_lo, t_hi = float(recon.t.min()), float(recon.t.max())
    tg = np.arange(t_lo, t_hi + 1e-9, 1.0)
    f = recon.smoothed.f
    xg = f(tg)
    vg = np.clip(f.derivative()(tg) * MPS_TO_MPH, 0, None)
    return tg, xg, vg


def _bearing_from_polyline(poly_latlon) -> float:
    """MapLibre camera bearing that makes start->end run left-to-right, so the
    route fills the wide-short map pane (ported from build_dashboard.py)."""
    lat0, lon0 = float(poly_latlon[0, 0]), float(poly_latlon[0, 1])
    lat1, lon1 = float(poly_latlon[-1, 0]), float(poly_latlon[-1, 1])
    mlat = math.cos(math.radians((lat0 + lat1) / 2))
    motion = (math.degrees(math.atan2((lon1 - lon0) * mlat, lat1 - lat0)) + 360.0) % 360.0
    return (motion - 90.0 + 360.0) % 360.0


def _cumdist_geodesic(poly_latlon):
    lat = poly_latlon[:, 0]
    lon = poly_latlon[:, 1]
    mlat = np.cos(np.radians((lat[:-1] + lat[1:]) / 2))
    dy = (lat[1:] - lat[:-1]) * 111320.0
    dx = (lon[1:] - lon[:-1]) * 111320.0 * mlat
    seg = np.hypot(dx, dy)
    return np.concatenate([[0.0], np.cumsum(seg)])


def shape_bundle(shape_id: str):
    """Return (shape, features, control_points, stops, facility_labels).

    shape feeds the ported MapView; features are stop/signal map markers;
    control_points + stops feed the pipeline's segment builder; facility_labels
    maps decompose_trip's facility_id strings to human labels for the bars.
    """
    poly, cum = load_gtfs_shape_with_dist(GTFS, shape_id)  # poly (N,2) lat,lon
    if cum is None:
        cum = _cumdist_geodesic(poly)
    lat, lon = poly[:, 0], poly[:, 1]

    def at(dist):
        return float(np.interp(dist, cum, lat)), float(np.interp(dist, cum, lon))

    stops_raw = load_route_stops(GTFS, shape_id)
    features = []
    facility_labels: dict[str, str] = {}
    for s in stops_raw:
        la, lo = at(s["dist_along_m"])
        sid = str(s["stop_id"])
        facility_labels[sid] = s["name"]  # dwell facility_id == raw stop_id
        features.append({"id": f"stop_{sid}", "kind": "bus_stop",
                         "label": s["name"], "cross_street": s["name"],
                         "dist_m": round(s["dist_along_m"], 1),
                         "lat": round(la, 6), "lon": round(lo, 6), "attributed": True})

    control_points = load_intersections(INTERSECTIONS).get(shape_id, []) if INTERSECTIONS.exists() else []
    for c in control_points:
        signalized = c.control_type in SIGNALIZED_CONTROL_TYPES
        label = " & ".join(c.cross_street_names) if c.cross_street_names else (
            "signal" if signalized else "crossing")
        if signalized:
            facility_labels[f"SIG_{c.intersection_node_id}"] = label
            la, lo = at(c.dist_along_route_m)
            features.append({"id": f"sig_{c.intersection_node_id}", "kind": "traffic_signals",
                             "label": label, "cross_street": label,
                             "dist_m": round(c.dist_along_route_m, 1),
                             "lat": round(la, 6), "lon": round(lo, 6), "attributed": True})
        else:
            facility_labels[f"CX_{c.intersection_node_id}"] = label

    shape = {
        "shape_id": shape_id,
        "polyline_lonlat": [[round(float(lo_), 6), round(float(la_), 6)] for la_, lo_ in poly],
        "cumdist_m": [round(float(d), 1) for d in cum],
        "bearing_deg": _bearing_from_polyline(poly),
        "bounds": [[float(lon.min()), float(lat.min())], [float(lon.max()), float(lat.max())]],
        "length_m": round(float(cum[-1]), 1),
    }
    return shape, features, control_points, stops_raw, facility_labels


def decompose_bands(recon, segments, facility_labels, offset_s):
    """Delay bands via the pipeline's decompose_trip — the exact attribution
    stack: signal-to-signal segments, dwell-zone (clipped at intersections) ->
    crossing -> signal-uniform -> slowdown, directional near-side flagging, and
    the slowdown->signal-overflow second pass."""
    if not segments:
        return []
    decomp = decompose_trip(to_pchip_record(recon), segments, _NO_FREEFLOW,
                            min_duration_s=DELAY_MIN_DURATION_S)
    bands = []
    for seg in decomp.segments:
        for a in seg.attributions:
            ev = a.event
            cat = a.category
            if cat == "dwell" and a.dwell_near_signal:
                cat = "dwell_near_signal"
            bands.append({
                "t_start": round(offset_s + ev.t_start, 1),
                "t_end": round(offset_s + ev.t_end, 1),
                "category": cat,
                "label": (facility_labels.get(a.facility_id, "") if a.facility_id else ""),
                "facility_id": a.facility_id,
                "seg_id": seg.seg_id,
                "min_v_mph": round(ev.min_v_mph, 1),
            })
    bands.sort(key=lambda b: b["t_start"])
    return bands


def source_payload(recon, offset_s, segments, facility_labels):
    """Curve (smoothed), raw on-route pings, speed, and attributed delay bands."""
    tg, xg, vg = dense_grid(recon)
    return {
        "anchor_offset_s": round(offset_s, 1),
        "curve": {
            "t": [round(offset_s + float(v), 1) for v in tg],
            "dist_m": [round(float(v), 1) for v in xg],
            "speed_mph": [round(float(v), 2) for v in vg],
        },
        "raw_pings": [
            {"t": round(offset_s + float(t), 1), "dist_m": round(float(d), 1)}
            for t, d in zip(recon.t, recon.d)
        ],
        "delays": decompose_bands(recon, segments, facility_labels, offset_s),
        "n_pings": int(recon.meta.n_pings),
        "n_on_route": int(recon.meta.n_on_route),
    }


# ------------------------------------------------------------------- build

def build_trip(key: str, token: str, manifest: pd.DataFrame, route_shapes: dict) -> dict | None:
    meta = json.loads(_pget(f"{key}/meta.json", token))
    route = str(meta["route_id"])
    bus_id, trip_id = str(meta["bus_id"]), str(meta["trip_id"])
    start_ms, end_ms = meta["start_t"], meta.get("end_t") or meta["start_t"] + 3_600_000

    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        # --- load phone pings, then pick the shape they actually follow ---
        (td / "phone.csv").write_bytes(_pget(f"{key}/pings.csv", token))
        phone_df = load_avl_csv(td / "phone.csv")
        phone_df = phone_df[phone_df["trip_id"] == trip_id]
        if phone_df.empty:
            phone_df = load_avl_csv(td / "phone.csv")  # trip_id quirk: take all
        shape_id = choose_shape(phone_df, route, route_shapes, shape_id_for_pattern(str(meta["pattern_id"])))
        pattern = str(int(shape_id[3:]))
        print(f"[{key}] rt {route} -> shape {shape_id} (pattern {pattern}; "
              f"meta said {meta['pattern_id']}) bus {bus_id} trip {trip_id}")

        try:
            shape, features, control_points, stops_dicts, facility_labels = shape_bundle(shape_id)
            matcher = matcher_for(shape_id)
        except KeyError as e:
            print(f"  SKIP: shape not in GTFS ({e})")
            return None
        segments = build_segments_from_records(control_points, stops_dicts)

        try:
            phone = reconstruct_trip(phone_df, matcher, bandwidth=20)
        except ValueError as e:
            print(f"  SKIP: phone reconstruct failed ({e})")
            return None

        # --- R2 @ bw=5 on the same shape, isolated to the ride's time-cluster ---
        r2_raw = trip_avl_pings(route, bus_id, trip_id, start_ms, end_ms, manifest=manifest)

        def recon_r2(pad_b, pad_a):
            rc = ride_cluster(r2_raw, start_ms, end_ms, pad_b, pad_a)
            if rc.empty:
                return None
            rc = rc.copy()
            rc["pattern_id"] = pattern
            rc.to_csv(td / "r2.csv", index=False)
            try:
                return reconstruct_trip(load_avl_csv(td / "r2.csv"), matcher, bandwidth=5)
            except ValueError:
                return None

        r2_recon = None
        if r2_raw.empty:
            print("  note: trip not found in R2 archive")
        else:
            # Generous window first (keeps the bus's run from its terminal); if
            # a reused trip_id contaminated it, fall back to the strict ride.
            r2_recon = recon_r2(90, 30)
            if _degenerate(r2_recon):
                strict = recon_r2(0, 0)
                if not _degenerate(strict):
                    print("  note: R2 used strict ride window (reused trip_id)")
                    r2_recon = strict
                elif r2_recon is None:
                    r2_recon = strict
            if _degenerate(r2_recon):
                print("  note: R2 reconstruction degenerate; dropping R2 layer")
                r2_recon = None

    # --- shared wall-clock axis (seconds since global t0, UTC) ---
    phone_anchor = anchor_ms(phone.meta.first_ping, CHICAGO)
    r2_anchor = anchor_ms(r2_recon.meta.first_ping, timezone.utc) if r2_recon else None

    events = list(csv.DictReader(io.StringIO(_pget(f"{key}/events.csv", token).decode())))
    ev_starts = [chicago_ms(e["start_time"]) for e in events if e.get("start_time")]

    t0 = min([phone_anchor] + ([r2_anchor] if r2_anchor else []) + ev_starts)

    # --- AVL stop/dwell layer (same vehicle), over the full ridden trip span
    # (incl. the pre-boarding portion the R2 layer also covers). The frontend
    # shows the pre-ride part only in time mode / full-trip view; in distance
    # mode it keeps just the stops the observed trajectory can place. ---
    phone_end = phone_anchor + float(phone.t.max()) * 1000
    r2_end = (r2_anchor + float(r2_recon.t.max()) * 1000) if r2_recon else None
    avl_lo = min([phone_anchor] + ([r2_anchor] if r2_recon else [])) - 300_000
    avl_hi = max([phone_end] + ([r2_end] if r2_recon else [])) + 300_000
    avl = avl_bands(bus_id, route, t0, avl_lo, avl_hi, facility_labels)

    payload = {
        "key": key,
        "route_id": route, "pattern_id": pattern, "trip_id": trip_id, "bus_id": bus_id,
        "destination": meta.get("destination", ""),
        "observer": meta.get("observer", ""),
        "t0_epoch_ms": t0,
        "route_length_m": shape["length_m"],
        "shape": shape,
        "features": features,
        "phone": source_payload(phone, (phone_anchor - t0) / 1000, segments, facility_labels),
        "r2": source_payload(r2_recon, (r2_anchor - t0) / 1000, segments, facility_labels) if r2_recon else None,
        "webapp_delays": [
            {
                "t_start": round((chicago_ms(e["start_time"]) - t0) / 1000, 1),
                "t_end": round((chicago_ms(e["end_time"]) - t0) / 1000, 1) if e.get("end_time") else None,
                "category": e["type"],
                "label": (e.get("stop_name") or e.get("note") or e["type"]),
            }
            for e in events if e.get("start_time")
        ],
        "avl_delays": avl,
    }
    n_web = len(payload["webapp_delays"])
    n_ph = len(payload["phone"]["delays"])
    n_r2 = len(payload["r2"]["delays"]) if payload["r2"] else "-"
    n_avl = len(avl)
    n_avl3 = sum(1 for b in avl if b["category"] == "avl_stop")
    print(f"  delays: webapp={n_web} phone={n_ph} r2={n_r2} avl={n_avl}({n_avl3} serviced); "
          f"r2_pings={r2_recon.meta.n_pings if r2_recon else 0}")
    return payload


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--token", default="ridethebus", help="webapp SYNC_TOKEN")
    ap.add_argument("--trips", nargs="*", help="explicit trip keys (default: auto-pick good GPS)")
    args = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest(refresh=True)
    route_shapes = route_shape_map(GTFS)
    keys = args.trips or good_trips(args.token)
    print(f"building {len(keys)} trip(s)\n")

    index = []
    for key in keys:
        try:
            payload = build_trip(key, args.token, manifest, route_shapes)
        except Exception as e:  # noqa: BLE001 — one bad trip shouldn't kill the batch
            print(f"[{key}] ERROR {e}")
            continue
        if payload is None:
            continue
        (OUT / f"{key}.json").write_text(json.dumps(payload))
        index.append({
            "key": key, "route_id": payload["route_id"], "trip_id": payload["trip_id"],
            "destination": payload["destination"],
            "label": f"Rt {payload['route_id']} → {payload['destination']} · {key.split('_')[1]}",
        })

    (OUT / "index.json").write_text(json.dumps({"trips": index}, indent=2))
    print(f"\nwrote {len(index)} trip(s) to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
