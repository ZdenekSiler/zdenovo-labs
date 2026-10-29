#!/usr/bin/env bash
# The whole "dark factory" schedule. Put it in cron and go to bed:
#
#   0 1 * * *  cd ~/zdenovo-labs/dark-factory && ./overnight.sh >> work/overnight.log 2>&1
#
# Night shift: run.py + local Ollama. No API spend, no network except the data download.
# Morning shift (optional): Claude Code, headless, reviews the run and writes REVIEW.md.
set -uo pipefail
cd "$(dirname "$0")"
echo "=== $(date -Is) night shift starts"

# Hard wall-clock limit: the night shift must be done before you wake up, finished or not.
python3 run.py run --deadline-min "${DEADLINE_MIN:-300}"
status=$?
echo "=== $(date -Is) night shift exit $status (0 = all gates passed)"

if command -v claude >/dev/null && [ "${SKIP_REVIEW:-0}" != "1" ]; then
  scripts/morning.sh
  echo "=== $(date -Is) morning review exit $?"
fi
exit $status
