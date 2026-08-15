"""Hard-coded exception registry for the network pipeline.

``exceptions.json`` (next to this module) is the single committed home for
every hand-curated override — the cases where the data is wrong or ambiguous
and a human decision beats any rule. Each entry records WHAT is overridden,
WHY, and the evidence, so the list stays auditable as it grows.

Entry types (the ``value`` schema each type expects):

  stop_coord_override   Force a stop pole's coordinate (bad GTFS geocode).
                        target: {"stop_id": str}
                        value:  {"lat": float, "lon": float, "source": str}

  door_peak_reject      Never use this stop's door-event peak as a location
                        (bad AVL stop attribution, e.g. peak lands at a
                        different stop).
                        target: {"stop_id": str}

  stop_segment_pin      Force a stop into a specific segment regardless of
                        projection (junction-interior judgment calls).
                        target: {"stop_id": str}
                        value:  {"seg_id": "SIG_<up>__SIG_<down>"}

  boundary_rep_override At a merged signal cluster, use this member node as
                        the boundary representative instead of the
                        first-in-route-order default.
                        target: {"junction_nodes": [int, ...]}
                        value:  {"rep_node": int}

  terminal_stop         Treat as an off-street terminal bay: excluded from
                        segment assignment and near/far-side classification.
                        target: {"stop_id": str}

  cluster_split         Force two node groups to remain SEPARATE
                        intersections even though they sit within the
                        clustering radius. Enforced as a dividing line in
                        both clustering layers: no cluster/component may
                        ever contain nodes from both sides (so third-party
                        nodes cannot bridge the divide transitively).
                        Unlisted nodes join whichever side they chain to.
                        target: {"nodes_a": [int, ...], "nodes_b": [int, ...]}

Common required fields per entry: ``id`` (unique slug), ``type``, ``city``,
``target``, ``why``, ``added`` (YYYY-MM-DD). Optional: ``value``,
``evidence``, ``review_after``.

Example entry::

    {
      "id": "cta-6515-bad-door-peak",
      "type": "door_peak_reject",
      "city": "cta",
      "target": {"stop_id": "6515"},
      "why": "Door peak lands on the 47th St bridge by the Red Line station,
              a block from the nominal 46th St pole - AVL stop attribution
              contamination, not a real service point.",
      "evidence": "2026-08 stop-location investigation, ambiguous5_review",
      "added": "2026-08-10"
    }

Consumers call ``load_exceptions(city_id)`` and read the typed indexes.
Wiring status: the pipeline does not consume these yet; hook points are
``registry.stops_by_shape`` (stop_coord_override, terminal_stop), the future
door-peak stop-location source (door_peak_reject), the stops_off assignment
in ``registry.build_registry`` (stop_segment_pin), and
``dataio.intersections.cluster_signals`` (boundary_rep_override).

Validate standalone with::

    PYTHONPATH=src uv run python analysis/network/exceptions.py
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

EXCEPTIONS_PATH = Path(__file__).resolve().parent / "exceptions.json"

_TYPES = {
    "stop_coord_override",
    "door_peak_reject",
    "stop_segment_pin",
    "boundary_rep_override",
    "terminal_stop",
    "cluster_split",
}
_COMMON_REQUIRED = ("id", "type", "city", "target", "why", "added")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class ExceptionsError(ValueError):
    """exceptions.json is malformed — fail fast, never guess."""


@dataclass(frozen=True)
class Exceptions:
    """Typed view of the entries for one city."""

    city: str
    coord_overrides: dict[str, tuple[float, float]] = field(default_factory=dict)
    peak_rejects: frozenset[str] = frozenset()
    segment_pins: dict[str, str] = field(default_factory=dict)
    boundary_reps: dict[frozenset[int], int] = field(default_factory=dict)
    terminal_stops: frozenset[str] = frozenset()
    cluster_splits: tuple[tuple[frozenset[int], frozenset[int]], ...] = ()
    entries: tuple[dict, ...] = ()


def _fail(entry_id: str | None, msg: str) -> None:
    where = f"entry {entry_id!r}" if entry_id else "exceptions.json"
    raise ExceptionsError(f"{where}: {msg}")


def _validate_entry(e: dict, seen_ids: set[str]) -> None:
    for k in _COMMON_REQUIRED:
        if not e.get(k):
            _fail(e.get("id"), f"missing required field {k!r}")
    eid = e["id"]
    if eid in seen_ids:
        _fail(eid, "duplicate id")
    seen_ids.add(eid)
    if e["type"] not in _TYPES:
        _fail(eid, f"unknown type {e['type']!r}; known: {sorted(_TYPES)}")
    if not _DATE_RE.match(e["added"]):
        _fail(eid, f"added={e['added']!r} is not YYYY-MM-DD")
    t, target, value = e["type"], e["target"], e.get("value") or {}
    if t in ("stop_coord_override", "door_peak_reject",
             "stop_segment_pin", "terminal_stop"):
        if not isinstance(target.get("stop_id"), str):
            _fail(eid, "target.stop_id must be a string")
    if t == "stop_coord_override":
        if not (isinstance(value.get("lat"), (int, float))
                and isinstance(value.get("lon"), (int, float))):
            _fail(eid, "value must have numeric lat and lon")
    if t == "stop_segment_pin":
        if not re.match(r"^SIG_\d+__SIG_\d+$", value.get("seg_id") or ""):
            _fail(eid, "value.seg_id must look like SIG_<up>__SIG_<down>")
    if t == "boundary_rep_override":
        nodes = target.get("junction_nodes")
        rep = value.get("rep_node")
        if (not isinstance(nodes, list) or len(nodes) < 2
                or not all(isinstance(n, int) for n in nodes)):
            _fail(eid, "target.junction_nodes must be a list of >=2 node ids")
        if rep not in nodes:
            _fail(eid, "value.rep_node must be one of target.junction_nodes")
    if t == "cluster_split":
        a, b = target.get("nodes_a"), target.get("nodes_b")
        for side, nm in ((a, "nodes_a"), (b, "nodes_b")):
            if (not isinstance(side, list) or not side
                    or not all(isinstance(n, int) for n in side)):
                _fail(eid, f"target.{nm} must be a non-empty list of node ids")
        if set(a) & set(b):
            _fail(eid, "nodes_a and nodes_b must be disjoint")


# Derived cities consume their parent's exceptions: cta-hf shares CTA's
# GTFS, caches, and door data, so CTA's stop/cluster facts apply verbatim.
_CITY_ALIASES = {"cta-hf": "cta"}


def load_exceptions(city_id: str, path: Path = EXCEPTIONS_PATH) -> Exceptions:
    city_id = _CITY_ALIASES.get(city_id, city_id)
    doc = json.loads(path.read_text())
    if doc.get("version") != 1:
        _fail(None, f"unsupported version {doc.get('version')!r}")
    seen: set[str] = set()
    coord, pins, reps = {}, {}, {}
    rejects, terminals = set(), set()
    splits = []
    kept = []
    for e in doc.get("entries", []):
        _validate_entry(e, seen)
        if e["city"] != city_id:
            continue
        kept.append(e)
        t, target, value = e["type"], e["target"], e.get("value") or {}
        if t == "stop_coord_override":
            coord[target["stop_id"]] = (float(value["lat"]), float(value["lon"]))
        elif t == "door_peak_reject":
            rejects.add(target["stop_id"])
        elif t == "stop_segment_pin":
            pins[target["stop_id"]] = value["seg_id"]
        elif t == "boundary_rep_override":
            reps[frozenset(target["junction_nodes"])] = value["rep_node"]
        elif t == "terminal_stop":
            terminals.add(target["stop_id"])
        elif t == "cluster_split":
            splits.append((frozenset(target["nodes_a"]),
                           frozenset(target["nodes_b"])))
    return Exceptions(
        city=city_id,
        coord_overrides=coord,
        peak_rejects=frozenset(rejects),
        segment_pins=pins,
        boundary_reps=reps,
        terminal_stops=frozenset(terminals),
        cluster_splits=tuple(splits),
        entries=tuple(kept),
    )


def load_all_cluster_splits(path: Path = EXCEPTIONS_PATH) -> dict[int, tuple[int, int]]:
    """node_id -> (divide_index, side) across ALL cities.

    OSM node ids are globally unique, so cluster_split divides are safe to
    apply city-agnostically — an entry for one city's nodes can never match
    another city's geometry. Both clustering layers consume this map.
    """
    doc = json.loads(path.read_text())
    seen: set[str] = set()
    out: dict[int, tuple[int, int]] = {}
    idx = 0
    for e in doc.get("entries", []):
        _validate_entry(e, seen)
        if e["type"] != "cluster_split":
            continue
        for side, nodes in enumerate((e["target"]["nodes_a"],
                                      e["target"]["nodes_b"])):
            for n in nodes:
                out[int(n)] = (idx, side)
        idx += 1
    return out


def main() -> int:
    doc = json.loads(EXCEPTIONS_PATH.read_text())
    seen: set[str] = set()
    for e in doc.get("entries", []):
        _validate_entry(e, seen)
    cities = sorted({e["city"] for e in doc.get("entries", [])})
    print(f"exceptions.json OK: {len(doc.get('entries', []))} entries"
          + (f" across {cities}" if cities else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
