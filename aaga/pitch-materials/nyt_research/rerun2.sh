#!/bin/zsh
cd "$(dirname "$0")"; PY="$(git rev-parse --show-toplevel)/.venv/bin/python"
for s in "mk_x.py daily" tsp_an.py ace.py twins.py rush_recs.py figdata.py figdata2.py figdata3.py figdata4.py; do echo "######## $s"; $PY -W ignore ${=s} 2>&1 | grep -v "▕" || echo FAILED $s; done; echo ALLDONE2
