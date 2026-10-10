#!/usr/bin/env bash
# Live evaluation batch: every scenario RUNS times, end to end, records to one directory.
#   scripts/run_evaluation.sh [runs]     (default 3; ~4 min per run)
set -uo pipefail
cd "$(dirname "$0")/.."
RUNS="${1:-3}"
OUT="docs/evaluation/live-$(date -u +%Y%m%dT%H%MZ)"
mkdir -p "$OUT"
export UV_CACHE_DIR="$PWD/.cache/uv" UV_PYTHON_INSTALL_DIR="$PWD/.cache/python" \
       UV_PYTHON_PREFERENCE=only-managed UV_PYTHON_INSTALL_BIN=0
for run in $(seq 1 "$RUNS"); do
  for s in A B C D E; do
    echo "=== run $run scenario $s"
    if [[ "$s" == "C" ]]; then
      uv run --frozen python -u scripts/run_scenario.py "$s" --out-dir "$OUT" || true
    else
      uv run --frozen python -u scripts/run_scenario.py "$s" --approve --out-dir "$OUT" || true
    fi
  done
done
uv run --frozen python scripts/summarize_evaluation.py "$OUT"
