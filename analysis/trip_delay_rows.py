"""Single-trip delay rows on the network pipeline's rules.

Replaces the proximity-based decomposition (dwell_near_signal /
signal_uniform / signal_overflow / crossing / slowdown) for the speed tab's
inferred rows. Each source curve — phone (high-freq) and R2 (low-freq) — is
run through ``core.decompose.door_delay.classify``, the same temporal rules
the network distributions use.

Rows emitted per source, as a clean partition of trip time:

    door   door open -> close                     (blue, stop-named)
    pre    > 10 s of <5 mph immediately before    (teal)
    post   > 10 s of <5 mph immediately after     (purple; post_ns when the
           post2 for swallowed cycles              stop is NEAR-SIDE)
    nd     slow with no door overlap, plus any
           sub-10 s shoulder that misses the rule (red, unlabelled)

Door cycles use the network filter (types 3/5 with
``ron+roff+fon+foff > 0``) so the two views count the same stops; stop
positions and near/far-side come from the registry, so attribution matches
the distributions rather than raw GTFS poles.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from core.decompose.door_delay import classify  # noqa: E402
from dataio.cities import get_city  # noqa: E402

_CACHE: dict = {}


def _stop_table(city, shape_id: str, month: str) -> tuple[dict, set]:
    """(stop_id -> along-shape metres, near-side stop ids) for one shape.

    Positions come from that month's registered (modal) door locations where
    available, converted from each segment's downstream-signal offset back to
    distance along the shape via the era's seg_bounds.
    """
    key = (shape_id, month)
    if key in _CACHE:
        return _CACHE[key]
    base = REPO / "outputs" / "network" / city.city_id
    seg_bounds = None
    for p in sorted((base / "era_shapes").glob("*.json")):
        try:
            recs = json.loads(p.read_text())
        except Exception:  # noqa: BLE001
            continue
        if shape_id in recs:
            seg_bounds = recs[shape_id]["seg_bounds"]
            break
    if seg_bounds is None:
        reg = json.loads((base / "segment_registry.json").read_text())
        rec = reg["shapes"].get(shape_id)
        seg_bounds = rec["seg_bounds"] if rec else []
    mp = base / "monthly_stops" / f"{month}.json"
    stops_by_seg = json.loads(mp.read_text()) if mp.exists() else {}
    if not stops_by_seg:
        reg = json.loads((base / "segment_registry.json").read_text())
        stops_by_seg = {s: r.get("stops_off", [])
                        for s, r in reg["segments"].items()}
    pos: dict[str, float] = {}
    near: set[str] = set()
    for seg_id, _x_lo, x_hi in seg_bounds:
        for st in stops_by_seg.get(seg_id, []):
            sid = str(st["id"])
            pos[sid] = float(x_hi) - float(st["off_m"])
            if st.get("signal_side") == "near_side":
                near.add(sid)
    _CACHE[key] = (pos, near)
    return pos, near


def _doors(city, bus_id: str, lo_ms: int, hi_ms: int) -> list[tuple]:
    """[(open_epoch_s, close_epoch_s, lat, lon), ...] for one vehicle window."""
    import duckdb

    if not city.door_source_dir:
        return []
    import pandas as pd
    d0 = pd.Timestamp(lo_ms, unit="ms", tz="UTC").tz_convert(city.tz)
    d1 = pd.Timestamp(hi_ms, unit="ms", tz="UTC").tz_convert(city.tz)
    months = sorted({d0.strftime("%Y%m"), d1.strftime("%Y%m")})
    src = Path(city.door_source_dir)
    files = [str(src / f"month={m}.parquet") for m in months
             if (src / f"month={m}.parquet").exists()]
    if not files:
        return []
    import pyarrow.parquet as pq
    cols = set(pq.ParquetFile(files[0]).schema.names)
    dwell = "dwell_s" if "dwell_s" in cols else "dwell_time"
    lst = ", ".join(f"'{f}'" for f in files)
    rows = duckdb.connect().execute(f"""
        SELECT epoch(event_time AT TIME ZONE '{city.tz}') AS t_open,
               coalesce({dwell}, 0) AS dwell_s, latitude, longitude,
               event_type,
               coalesce(fon,0) AS fon, coalesce(ron,0) AS ron,
               coalesce(foff,0) AS foff, coalesce(roff,0) AS roff,
               passenger_load
        FROM read_parquet([{lst}], union_by_name=true)
        WHERE CAST(bus_id AS VARCHAR) = '{bus_id}'
          AND coalesce(ron,0)+coalesce(roff,0)
              +coalesce(fon,0)+coalesce(foff,0) > 0
          AND epoch(event_time AT TIME ZONE '{city.tz}')
              BETWEEN {lo_ms / 1000.0} AND {hi_ms / 1000.0}
        ORDER BY t_open""").fetchall()
    out = []
    for a, b, c, d, et, fon, ron, foff, roff, load in rows:
        on, off = int(fon) + int(ron), int(foff) + int(roff)
        after = int(load) if load is not None else None
        out.append({
            "open": float(a), "close": float(a) + float(b),
            "lat": float(c or 0), "lon": float(d or 0),
            "dwell_s": round(float(b), 1), "event_type": int(et),
            "on_front": int(fon), "on_rear": int(ron),
            "off_front": int(foff), "off_rear": int(roff),
            "on_total": on, "off_total": off, "flow": on + off,
            "load_after": after,
            "load_before": (after - (on - off)) if after is not None else None,
        })
    return out


def build_rows(obs: dict, city_id: str = "cta") -> tuple[list, set]:
    """(delay_rows for the trip, stop_ids referenced) on the new logic."""
    city = get_city(city_id)
    t0_ms = obs.get("t0_epoch_ms") or 0
    bus = str(obs.get("bus_id") or "")
    shape_id = str(obs.get("shape", {}).get("shape_id") or "")
    poly = obs["shape"]["polyline_lonlat"]
    cum = obs["shape"]["cumdist_m"]

    # widest source span, padded, in epoch seconds
    spans = []
    for k in ("phone", "r2"):
        s = obs.get(k)
        if s and s.get("curve", {}).get("t"):
            t = s["curve"]["t"]
            spans += [t[0], t[-1]]
    if not spans:
        return [], set()
    lo_ms = int(t0_ms + min(spans) * 1000) - 300_000
    hi_ms = int(t0_ms + max(spans) * 1000) + 300_000
    doors = _doors(city, bus, lo_ms, hi_ms)

    month = None
    if doors:
        import pandas as pd
        month = pd.Timestamp(doors[0]["open"], unit="s",
                             tz="UTC").tz_convert(city.tz).strftime("%Y%m")
    pos, near = _stop_table(city, shape_id, month or "") if shape_id else ({}, set())

    # attribute each cycle to the nearest registered stop along the shape
    from scipy.spatial import cKDTree
    arr = np.asarray(poly, float)          # lon, lat
    mlat = 111320.0 * np.cos(np.radians(float(arr[:, 1].mean())))
    xy = np.column_stack([arr[:, 0] * mlat, arr[:, 1] * 111320.0])
    tree = cKDTree(xy)
    cumd = np.asarray(cum, float)
    ids = list(pos)
    sdist = np.array([pos[s] for s in ids]) if ids else np.empty(0)
    stop_ids: list = []
    for dc in doors:
        lat, lon = dc["lat"], dc["lon"]
        if not ids:
            stop_ids.append(None)
            continue
        _d, i = tree.query([lon * mlat, lat * 111320.0])
        x = float(cumd[min(i, len(cumd) - 1)])
        stop_ids.append(ids[int(np.argmin(np.abs(sdist - x)))])
    names = {str(f["id"]).split("_")[-1]: f.get("label")
             for f in obs.get("features", []) if f.get("kind") == "bus_stop"}

    rows, referenced = [], set()
    door_items = [
        {"t_start": round(dc["open"] - t0_ms / 1000.0, 1),
         "t_end": round(dc["close"] - t0_ms / 1000.0, 1),
         "category": "door", "stop_id": s,
         "label": names.get(str(s)) or (f"stop {s}" if s else "door"),
         # keeps the rich passenger tooltip working on the Door events row
         "event_type": dc["event_type"],
         "event_desc": "Serviced stop" if dc["event_type"] == 3 else "Unknown stop",
         "dwell_s": dc["dwell_s"], "flow": dc["flow"],
         "on_total": dc["on_total"], "on_front": dc["on_front"],
         "on_rear": dc["on_rear"], "off_total": dc["off_total"],
         "off_front": dc["off_front"], "off_rear": dc["off_rear"],
         "load_before": dc["load_before"], "load_after": dc["load_after"],
         "dwell_per_pax": (round(dc["dwell_s"] / dc["flow"], 1)
                           if dc["flow"] else None)}
        for dc, s in zip(doors, stop_ids)
    ]
    rows.append({"key": "door", "label": "Door events", "role": "door",
                 "source_key": "phone", "items": door_items})
    referenced |= {str(s) for s in stop_ids if s}

    for key, label in (("phone", "High-Freq"), ("r2", "Low-Freq")):
        src = obs.get(key)
        if not src or not src.get("curve", {}).get("t"):
            continue
        t = np.asarray(src["curve"]["t"], float)
        x = np.asarray(src["curve"]["dist_m"], float)
        abs_t = t + t0_ms / 1000.0
        pieces = classify(abs_t, x, [(d["open"], d["close"]) for d in doors],
                          stop_ids=stop_ids, stop_names=names, near_side=near,
                          emit_short_shoulders=True)
        items = [
            {"t_start": round(p.t_start - t0_ms / 1000.0, 1),
             "t_end": round(p.t_end - t0_ms / 1000.0, 1),
             "category": p.render_cls, "stop_id": p.stop_id,
             "near_side": p.near_side,
             "label": p.stop_name or ("" if p.cls == "nd" else f"stop {p.stop_id}")}
            for p in pieces if p.cls != "dw"      # dw is not shown here
        ]
        rows.append({"key": key, "label": label, "role": "inferred",
                     "source_key": key, "items": items})
        referenced |= {str(p.stop_id) for p in pieces if p.stop_id}
    return rows, referenced
