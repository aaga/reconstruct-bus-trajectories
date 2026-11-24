#!/bin/zsh
cd "$(dirname "$0")"; PY="$(git rev-parse --show-toplevel)/.venv/bin/python"
$PY period.py ref && $PY tsp.py && $PY jobs.py win && $PY bundle.py && $PY aligned.py && $PY jobs.py day && echo ALLDONE
