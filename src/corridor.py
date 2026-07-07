"""The transit corridor this study analyzes, in one place.

The reconstruction/decomposition pipeline and the figure scripts are otherwise
route-agnostic — they read these constants instead of hard-coding CTA Route 22.
To retarget the whole pipeline to another route, change these values and provide
that route's intersection-enrichment cache (``INTERSECTIONS_FILE``).

Importable as ``import corridor`` anywhere ``src`` is on the path (the pyproject
``pythonpath`` and every pipeline script's ``sys.path.insert(REPO / "src")``).
"""

from __future__ import annotations

ROUTE_ID = "22"                # CTA route
PATTERN_ID = "3936"            # southbound GTFS pattern
SHAPE_ID = "67803936"          # GTFS shape for PATTERN_ID
DIRECTION = "sb"               # southbound
CORRIDOR_NAME = "Route 22 SB"  # human label for titles / axes

# A representative full-length trip, used as the default target in per-trip
# figures. TRIP_LABEL is the leading service id.
REFERENCE_TRIP_ID = "1001350_4017_2026-05-05"
REFERENCE_TRIP_LABEL = REFERENCE_TRIP_ID.split("_")[0]

# Precomputed OSM intersection enrichment for SHAPE_ID (repo-root-relative).
INTERSECTIONS_FILE = "intersections_route22.json"
