#!/bin/zsh
# Full historical CTA pass: 2024-01-01 .. 2026-08-06 (949 service dates).
#
# Every stage skips work whose output already exists, so this script is
# safe to re-run and resumes where it stopped. Stages are ordered by
# dependency; a stage that fails aborts the run rather than corrupting
# what follows.
#
#   usage: analysis/network/run_history.sh [START] [END]
set -u
cd "$(dirname "$0")/../.." || exit 1

START=${1:-2024-01-01}
END=${2:-2026-08-06}
CITY=cta
WORKERS=6
LOG=${HISTORY_LOG:-/tmp/history_run.log}
MIN_FREE_GB=6

run() {   # run <label> <cmd...>
  echo "=== $1  $(date '+%F %T') ===" >> "$LOG"
  local free
  free=$(df -g . | tail -1 | awk '{print $4}')
  if [ "$free" -lt "$MIN_FREE_GB" ]; then
    echo "ABORT: only ${free} GB free (need ${MIN_FREE_GB})" >> "$LOG"
    exit 1
  fi
  shift
  if ! PYTHONPATH=src uv run python "$@" >> "$LOG" 2>&1; then
    echo "STAGE FAILED: $*" >> "$LOG"
    exit 1
  fi
}

echo "######## history run $START .. $END  $(date) ########" >> "$LOG"

# 1. GTFS eras: map each feed version's shapes onto the canonical segments.
run "era_seg_bounds" analysis/network/era_seg_bounds.py --city $CITY --all-eras

# 2. Monthly registered stop locations from door events.
run "monthly_stops" analysis/network/monthly_stops.py --city $CITY \
    --months "${START:0:7}:${END:0:7}"

# 3. Trajectory reconstruction -> per-traversal segment times.
run "reconstruct" analysis/network/run_reconstruct.py --city $CITY \
    --start "$START" --end "$END" --workers $WORKERS

# 4. Delay events + per-bucket trajectory crossing times (5 mph pass).
run "delay_events" analysis/network/delay_events.py --city $CITY \
    --start "$START" --end "$END" --workers $WORKERS --traj-speed

# 5. Aggregates.
run "stop_stats"     analysis/network/build_stop_stats.py --city $CITY
run "distributions"  analysis/network/build_distributions.py --city $CITY

echo "######## ALL DONE $(date) ########" >> "$LOG"
