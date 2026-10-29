#!/usr/bin/env bash
# Morning shift: headless Claude Code reviews the night's run and writes work/out/REVIEW.md.
#
# Permissions are passed on the command line, not only via .claude/settings.json. A project's
# allow rules are ignored until someone has accepted the trust dialog in that folder
# interactively. That's a good security default, and a terrible surprise in a cron job: the
# first headless run here got "Ignoring 8 permissions.allow entries ... workspace has not been
# trusted", couldn't read anything, and (correctly) reported "Verdict: UNKNOWN".
set -uo pipefail
cd "$(dirname "$0")/.."

ALLOW=(
  "Bash(python3 run.py status:*)" "Bash(python3 run.py report:*)"
  "Bash(python3 run.py requeue:*)" "Bash(python3 run.py run:*)"
  "Bash(sqlite3 work/factory.db:*)" "Bash(ollama list:*)"
  "Read" "Edit(work/out/REVIEW.md)" "Write(work/out/REVIEW.md)"
)
DENY=(
  "Edit(factory/checks.py)" "Write(factory/checks.py)"
  "Bash(rm:*)" "Bash(git push:*)" "Bash(curl:*)" "WebFetch" "WebSearch"
  "Bash(scripts/chaos.sh:*)" "Bash(python3 run.py blackout:*)"
)

# --max-turns caps the bill. The Stop hook (if the folder is trusted) and the prompt both
# require REVIEW.md before the session ends.
claude -p "Run the morning review described in .claude/commands/morning-review.md" \
  --allowedTools "${ALLOW[@]}" --disallowedTools "${DENY[@]}" \
  --max-turns 40 --output-format json > work/out/claude-review.json
