#!/bin/zsh
# Overnight pass, 2026-08-24: monthly re-registration (pattern-restricted),
# full delay_events rebuild (layover fix + near_side + traj-speed + headway
# CV), aggregates, door sidecar, pick-free payloads, CV payloads, single-trip
# refresh, ping_density. Every stage skips existing work or is cheap, so
# re-running the script resumes.
set -u
cd "$(dirname "$0")/../.." || exit 1

CITY=cta
WORKERS=6
LOG=${OVERNIGHT_LOG:-/tmp/overnight_run.log}
MIN_FREE_GB=6

run() {   # run <label> <cmd...>
  echo "=== $1  $(date '+%F %T') ===" >> "$LOG"
  local free
  free=$(df -g . | tail -1 | awk '{print $4}')
  if [ "$free" -lt "$MIN_FREE_GB" ]; then
    echo "ABORT: only ${free} GB free" >> "$LOG"; exit 1
  fi
  shift
  if ! PYTHONPATH=src uv run python "$@" >> "$LOG" 2>&1; then
    echo "STAGE FAILED: $*" >> "$LOG"; exit 1
  fi
}

echo "######## overnight run $(date) ########" >> "$LOG"

# 1. Monthly stop registration, pattern-restricted (all months, forced).
run "monthly_stops" analysis/network/monthly_stops.py --city $CITY \
    --months 2024-01:2026-08 --force

# 2. Full delay_events: layover fix + near_side + traj_speed + headway CV.
run "delay_events" analysis/network/delay_events.py --city $CITY \
    --workers $WORKERS --force --traj-speed --headway-cv

# 3. Aggregates over the rebuilt events.
run "distributions" analysis/network/build_distributions.py --city $CITY
run "stop_stats"    analysis/network/build_stop_stats.py --city $CITY
run "facts"         analysis/network/build_facts.py --city $CITY

# 4. Door sidecar for all 957 dates (rebuilt: layover guard changed sums).
run "door_join" analysis/network/door_join.py --city $CITY --force

# 5. Map payloads (pick-free, month-folded).
run "payloads" analysis/network/build_payloads.py --city $CITY

# 6. Headway-CV payloads.
run "cv" analysis/network/build_cv.py --city $CITY

# 7. Single-trip payloads on the re-registered stops.
run "single_trip" analysis/build_dashboard_data.py

# 8. Raw ping density (direct-read; feeds the raw-pings tab).
run "ping_density" analysis/network/ping_density.py --city $CITY

echo "######## OVERNIGHT DONE $(date) ########" >> "$LOG"
