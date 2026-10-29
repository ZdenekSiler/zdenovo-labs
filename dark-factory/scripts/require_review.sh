#!/usr/bin/env bash
# Claude Code Stop hook: refuse to let the morning supervisor finish until it has written
# work/out/REVIEW.md, newer than the morning report it's reviewing.
# Exit 2 = block the stop and show stderr to Claude. Exit 0 = allow.
set -euo pipefail
cd "${CLAUDE_PROJECT_DIR:-.}"

# Already blocked once and Claude is still trying to stop: let it go, or we loop forever.
if python3 -c 'import json,sys; sys.exit(0 if json.load(sys.stdin).get("stop_hook_active") else 1)'; then
  exit 0
fi

report=work/out/MORNING_REPORT.md
review=work/out/REVIEW.md
[ -f "$report" ] || exit 0  # nothing ran, nothing to review

if [ ! -f "$review" ] || [ "$review" -ot "$report" ]; then
  echo "Write $review before stopping: verdict, actions taken, decisions a human must make." >&2
  exit 2
fi
exit 0
