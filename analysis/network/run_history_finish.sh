#!/bin/zsh
# Post-run finish: re-do the dates that predate the historical pipeline, then
# rebuild every aggregate over the uniform 2.5 years.
#
# The 2026-05-01..2026-08-01 window was built by the original 93-day run
# against the bundled GTFS snapshot (763 bus shapes, no seasonal routes) with
# stamp-derived stop positions. run_history.sh skips existing checkpoints, so
# those dates would keep the old method and leave a seam exactly where a trend
# analysis looks. --force re-runs them through the era pipeline.
set -u
cd "$(dirname "$0")/../.." || exit 1

CITY=cta
WORKERS=6
REDO_START=${1:-2026-05-01}
REDO_END=${2:-2026-08-01}
LOG=${FINISH_LOG:-/tmp/history_finish.log}

run() {
  echo "=== $1  $(date '+%F %T') ===" >> "$LOG"
  shift
  if ! PYTHONPATH=src uv run python "$@" >> "$LOG" 2>&1; then
    echo "STAGE FAILED: $*" >> "$LOG"; exit 1
  fi
}

echo "######## finish pass $(date) ########" >> "$LOG"

run "redo-reconstruct" analysis/network/run_reconstruct.py --city $CITY \
    --start "$REDO_START" --end "$REDO_END" --workers $WORKERS --force
run "redo-events" analysis/network/delay_events.py --city $CITY \
    --start "$REDO_START" --end "$REDO_END" --workers $WORKERS --force --traj-speed

# Aggregates over the now-uniform series.
run "stop_stats"    analysis/network/build_stop_stats.py --city $CITY
run "distributions" analysis/network/build_distributions.py --city $CITY
run "facts"         analysis/network/build_facts.py --city $CITY

echo "######## FINISH DONE $(date) ########" >> "$LOG"
