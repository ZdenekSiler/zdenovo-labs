#!/usr/bin/env bash
# Headless Claude Code with ONE MCP server: the blog-analytics server, sandboxed.
#   live/claude_code.sh data/snapshot.db "Which article is the most read?"
# Uses your Claude Code login. Hard spending cap via --max-budget-usd.
set -euo pipefail
LAB="$(cd "$(dirname "$0")/.." && pwd)"
DB="$(realpath "${1:?usage: claude_code.sh <blog.db> <question>}")"
CFG="$(mktemp --suffix=.json)"
trap 'rm -f "$CFG"' EXIT
cat > "$CFG" <<JSON
{"mcpServers": {"blog": {"command": "$LAB/sandbox/run.sh", "args": ["$DB"]}}}
JSON
cd "$(mktemp -d)"   # an empty directory: no project files, no CLAUDE.md
claude -p "$2" \
  --mcp-config "$CFG" --strict-mcp-config \
  --tools "" \
  --allowedTools "mcp__blog" \
  --setting-sources "" \
  --no-session-persistence \
  --max-budget-usd 0.40 \
  --output-format stream-json --verbose
