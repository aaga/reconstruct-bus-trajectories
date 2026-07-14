"""Build the merged dashboard's unified data payloads + catalog.

One payload schema, ``kind: "trip" | "aggregate"``, with a shared
``shape`` + ``features`` core. Trips carry a ``sources[]`` list (each with a raw
dense ``curve``) and ``delay_rows[]``; aggregates carry ``segments[]`` plus
per-facility stats folded onto ``features``.

Inputs:
  * observation trips  ← outputs/obs_trips/*.json  (converted from comparison.py)
  * route aggregate    ← analysis.route_aggregate.build_route_aggregate()
    (computed directly from the decomposition inputs — no intermediate file)

Run:  uv run python analysis/build_dashboard_data.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))  # so `import corridor` + `import analysis.*` resolve

import corridor  # noqa: E402
from analysis.route_aggregate import build_route_aggregate  # noqa: E402

OBS_DATA = REPO / "outputs" / "obs_trips"
OUT = REPO / "dashboard" / "data"


def _trailing_digits(s) -> str | None:
    m = re.findall(r"\d+", str(s))
    return m[-1] if m else None

SOURCE_STYLE = {
    "phone": {"label": "High-Freq", "color": "#2e9e4f"},
    "r2": {"label": "Low-Freq", "color": "#f4b400"},
}


def _source(key: str, src: dict) -> dict:
    """One obs source block (phone/r2) → unified source entry (raw dense curve)."""
    style = SOURCE_STYLE[key]
    return {
        "key": key,
        "label": style["label"],
        "color": style["color"],
        "anchor_offset_s": src.get("anchor_offset_s", 0),
        "curve": src["curve"],           # {t, dist_m, speed_mph} — raw dense, unchanged
        "raw_pings": src.get("raw_pings", []),
        "n_pings": src.get("n_pings", 0),
        "n_on_route": src.get("n_on_route", 0),
    }


def trip_payload(obs: dict, label: str) -> dict:
    """Convert one observation-schema trip JSON to the unified trip schema."""
    sources = [_source("phone", obs["phone"])]
    if obs.get("r2"):
        sources.append(_source("r2", obs["r2"]))

    # delay_rows generalise phone.delays / webapp_delays / avl_delays. role drives
    # rendering (avl → rich passenger tooltip; observed/inferred → standard tip).
    delay_rows = [
        {"key": "avl", "label": "AVL", "role": "avl", "source_key": "phone",
         "items": obs.get("avl_delays", [])},
        {"key": "observed", "label": "Observed", "role": "observed", "source_key": "phone",
         "items": obs.get("webapp_delays", [])},
        {"key": "phone", "label": "High-Freq", "role": "inferred", "source_key": "phone",
         "items": obs["phone"]["delays"]},
    ]
    if obs.get("r2"):
        delay_rows.append(
            {"key": "r2", "label": "Low-Freq", "role": "inferred", "source_key": "r2",
             "items": obs["r2"]["delays"]})

    # A feature "has delay" this trip if an inferred delay references it (matched
    # by trailing id digits — feature ids like sig_<node>/stop_<sid> vs facility
    # ids like SIG_<node>/<sid>). Overrides the obs bundle's blanket attributed=True.
    referenced = {
        _trailing_digits(it.get("facility_id"))
        for row in delay_rows if row["role"] == "inferred"
        for it in row["items"] if it.get("facility_id")
    }
    referenced.discard(None)
    features = [{**f, "attributed": _trailing_digits(f["id"]) in referenced}
                for f in obs["features"]]

    return {
        "kind": "trip",
        "key": obs["key"],
        "label": label,
        "route_id": obs.get("route_id"),
        "pattern_id": obs.get("pattern_id"),
        "trip_id": obs.get("trip_id"),
        "bus_id": obs.get("bus_id"),
        "destination": obs.get("destination"),
        "observer": obs.get("observer"),
        "t0_epoch_ms": obs.get("t0_epoch_ms"),
        "shape": obs["shape"],
        "features": features,
        "sources": sources,
        "delay_rows": delay_rows,
    }


def aggregate_payload(route: dict, *, key: str, route_id: str) -> dict:
    """Convert the route-aggregate JSON to the unified aggregate schema."""
    return {
        "kind": "aggregate",
        "key": key,
        "label": route.get("view_title", key),
        "view_title": route.get("view_title", key),  # DelayView reads this
        "route_id": route_id,
        "n_trips": route.get("n_trips"),
        "shape": route["shape"],
        "features": route["features"],   # already carry mean_min/p95_min/buffer_min/attributed
        "segments": route["segments"],
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    items: list[dict] = []

    # --- trips (from the observation dashboard bundle) ---
    obs_index = json.loads((OBS_DATA / "index.json").read_text())
    labels = {t["key"]: t["label"] for t in obs_index["trips"]}
    for t in obs_index["trips"]:
        obs = json.loads((OBS_DATA / f"{t['key']}.json").read_text())
        payload = trip_payload(obs, labels.get(obs["key"], obs["key"]))
        (OUT / f"{obs['key']}.json").write_text(json.dumps(payload, separators=(",", ":")))
        items.append({"key": payload["key"], "kind": "trip", "label": payload["label"],
                      "route_id": payload["route_id"], "trip_id": payload["trip_id"]})

    # --- route aggregate (computed directly from the decomposition inputs) ---
    try:
        route = build_route_aggregate()
    except FileNotFoundError as exc:
        route = None
        print(f"[dashboard-data] skipping aggregate — {exc}")
    if route:
        agg = aggregate_payload(route, key="sb_route", route_id=corridor.ROUTE_ID)
        (OUT / "sb_route.json").write_text(json.dumps(agg, separators=(",", ":")))
        items.append({"key": agg["key"], "kind": "aggregate", "label": agg["label"],
                      "route_id": agg["route_id"], "n_trips": agg["n_trips"]})

    (OUT / "index.json").write_text(json.dumps({"items": items}, indent=2))
    print(f"[dashboard-data] wrote {len(items)} payloads to {OUT}")
    for it in items:
        print(f"    {it['kind']:9} {it['key']}  ({it['label']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
