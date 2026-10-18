#!/usr/bin/env bash
# Regenerates every transcript the series quotes. Offline, ~10 s. Key IDs differ per run.
set -euo pipefail
cd "$(dirname "$0")"
for d in part1_tour part2_contract part3_resilience part4_cost part5_security part6_guardrails \
         part7_observability part8_gameday part8_checks; do
  uv run python -m "demos.$d" > "outputs/$d.txt" 2>&1
  echo "ok  outputs/$d.txt"
done
uv run pytest -q > outputs/pytest.txt 2>&1 && tail -1 outputs/pytest.txt
