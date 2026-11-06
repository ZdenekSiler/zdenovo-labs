#!/usr/bin/env bash
# Regenerate every offline transcript the series quotes, then run the tests. No API key needed.
# The sandbox demo runs if Docker is available; the snapshot tour only on the author's machine.
set -euo pipefail
cd "$(dirname "$0")"
uv run python -m blogmcp.fixture data/fixture.db
for d in part1_hello part2_server part3_design part5_clients part6_pitfalls part6_http; do
  uv run python -m "demos.$d" > "outputs/$d.txt" 2>/dev/null
  echo "ok    outputs/$d.txt"
done
if command -v docker >/dev/null && docker info >/dev/null 2>&1; then
  sandbox/build.sh >/dev/null
  uv run python -m demos.part4_sandbox > outputs/part4_sandbox.txt 2>/dev/null && echo "ok    outputs/part4_sandbox.txt"
else
  echo "skip  part4_sandbox (no docker)"
fi
if [ -f data/snapshot.db ]; then
  uv run python -m demos.snapshot_tour > outputs/snapshot_tour.txt 2>/dev/null && echo "ok    outputs/snapshot_tour.txt"
fi
uv run pytest -q 2>&1 | tail -1
