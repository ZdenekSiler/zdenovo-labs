#!/usr/bin/env bash
# Build the sandbox image.
set -euo pipefail
cd "$(dirname "$0")/.."
docker build -q -f sandbox/Dockerfile -t blogmcp:lab .
