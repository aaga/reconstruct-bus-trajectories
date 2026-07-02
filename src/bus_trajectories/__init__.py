"""TEMPORARY compatibility shim (Phase 2 of the reorg).

Aliases the old ``bus_trajectories.*`` module names to their new homes under the
flat ``core`` / ``dataio`` / ``viz`` / ``cli`` packages, so existing
``from bus_trajectories... import`` statements in scripts and tests keep working
until they are migrated to the new paths (Phase 3). Delete this whole package
once no consumer imports ``bus_trajectories`` anymore.
"""

from __future__ import annotations

import importlib
import sys

# old dotted suffix (under bus_trajectories.) -> new fully-qualified module
_MAP = {
    "smooth": "core.smooth",
    "pipeline": "core.pipeline",
    "serialize": "core.serialize",
    "mapmatch": "core.mapmatch",
    "mapmatch.shape_snap": "core.mapmatch.shape_snap",
    "mapmatch.valhalla": "core.mapmatch.valhalla",
    "delay_decomposition": "core.delay_decomposition",
    "delay_decomposition.attribution": "core.delay_decomposition.attribution",
    "delay_decomposition.decompose": "core.delay_decomposition.decompose",
    "delay_decomposition.dwell": "core.delay_decomposition.dwell",
    "delay_decomposition.events": "core.delay_decomposition.events",
    "delay_decomposition.feature_attribution": "core.delay_decomposition.feature_attribution",
    "delay_decomposition.loss": "core.delay_decomposition.loss",
    "delay_decomposition.segments": "core.delay_decomposition.segments",
    "delay_decomposition.travel_time": "core.delay_decomposition.travel_time",
    "io": "dataio.io",
    "realtime": "dataio.realtime",
    "intersections": "dataio.intersections",
    "way_match": "dataio.way_match",
    "vtrak": "dataio.vtrak",
    "colors": "viz.colors",
    "plot": "viz.plot",
    "viz": "viz.viz",
    "viz_compare": "viz.viz_compare",
    "cli": "cli.cli",
}

__version__ = "0.1.0"

_self = sys.modules[__name__]
for _suffix, _new in _MAP.items():
    _mod = importlib.import_module(_new)
    sys.modules[f"{__name__}.{_suffix}"] = _mod
    if "." not in _suffix:  # expose top-level names as attributes too
        setattr(_self, _suffix, _mod)
