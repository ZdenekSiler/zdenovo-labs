#!/usr/bin/env bash
# Run the MCP server over stdio inside a locked-down container.
#
#   sandbox/run.sh data/fixture.db                 # the blog-analytics server
#   sandbox/run.sh data/fixture.db naughty         # the probe server, same sandbox
#
# An MCP host starts this script as the server command; stdin/stdout are the transport.
set -euo pipefail
DB="$(realpath "${1:?usage: run.sh <blog.db> [naughty]}")"
ENTRY=()
[ "${2:-}" = "naughty" ] && ENTRY=(--entrypoint python blogmcp:lab sandbox/naughty_server.py)
[ ${#ENTRY[@]} -eq 0 ] && ENTRY=(blogmcp:lab)

exec docker run -i --rm \
  --network none \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=8m \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --pids-limit 64 \
  --memory 256m --memory-swap 256m \
  --cpus 1 \
  --user 10001:10001 \
  --mount "type=bind,source=$DB,target=/data/blog.db,readonly" \
  -e BLOG_DB_IMMUTABLE=1 \
  --log-driver none \
  "${ENTRY[@]}"
