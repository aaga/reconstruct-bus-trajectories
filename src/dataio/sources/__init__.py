"""Pluggable GPS-trace sources → one canonical trace.

This is the seam decision #2 of the reorg calls for. Every adapter's ``load(spec)``
returns a DataFrame in the *canonical trace* shape — exactly what
:func:`core.reconstruct.reconstruct_trip` consumes — so the reconstruction /
decomposition / dashboard pipeline is source-agnostic:

    avl_event_time   datetime64   (one row per ping, sorted per trip)
    latitude         float
    longitude        float
    trip_id          str
    bus_id           str
    route_id         str
    pattern_id       str

``load_trace(spec)`` dispatches on ``spec["kind"]``:

    ``phone_csv``     a phone-GPS CSV (the Record-a-Ride export shape)
    ``avl_archive``   the public R2 realtime archive (one observed trip)
    ``pages_backup``  the Record-a-Ride Pages backup API (rich; network)
    ``vtrak``         a dense VTRAK/ROCKET location list

The simplified dashboard consumes ``avl_archive``; the rich dashboard consumes
``pages_backup`` (+ AVL). ``analysis.build_dashboard_data`` is the intended
orchestrator (``load_trace → reconstruct_trip → decompose_trip → payload``).
"""

from __future__ import annotations

import importlib

import pandas as pd

CANONICAL_COLUMNS = [
    "avl_event_time",
    "latitude",
    "longitude",
    "trip_id",
    "bus_id",
    "route_id",
    "pattern_id",
]

# spec["kind"] → submodule exposing load(spec) -> DataFrame
_LOADERS = {
    "phone_csv": "phone_csv",
    "avl_archive": "avl_archive",
    "pages_backup": "pages_backup",
    "vtrak": "vtrak",
}


def ensure_canonical(df: pd.DataFrame) -> pd.DataFrame:
    """Validate + coerce ``df`` to the canonical trace contract.

    Raises ``ValueError`` if a required column is missing. ``avl_event_time`` is
    coerced to ``datetime64`` (accepts the archive's ``%Y-%m-%d %H:%M:%S.%f``
    strings or already-parsed datetimes) so the result feeds ``reconstruct_trip``
    directly.
    """
    missing = [c for c in CANONICAL_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"trace is missing canonical columns: {missing}")
    out = df[CANONICAL_COLUMNS].copy()
    out["avl_event_time"] = pd.to_datetime(out["avl_event_time"])
    out["latitude"] = out["latitude"].astype(float)
    out["longitude"] = out["longitude"].astype(float)
    for c in ("trip_id", "bus_id", "route_id", "pattern_id"):
        out[c] = out[c].astype(str)
    return out.sort_values(["trip_id", "avl_event_time"]).reset_index(drop=True)


def empty_canonical() -> pd.DataFrame:
    """An empty frame with the canonical columns (for not-found lookups)."""
    return pd.DataFrame({c: pd.Series(dtype="object") for c in CANONICAL_COLUMNS})


def load_trace(spec: dict) -> pd.DataFrame:
    """Load a canonical trace from any registered source. ``spec["kind"]`` selects
    the adapter; the remaining keys are adapter-specific."""
    kind = spec.get("kind")
    if kind not in _LOADERS:
        raise ValueError(f"unknown trace source {kind!r}; known: {sorted(_LOADERS)}")
    mod = importlib.import_module(f"{__name__}.{_LOADERS[kind]}")
    return ensure_canonical(mod.load(spec))
