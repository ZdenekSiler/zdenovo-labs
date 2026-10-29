#!/usr/bin/env bash
# Chaos drills that happen *outside* the workers, against a run that's already going.
#
#   scripts/chaos.sh freeze 90     SIGSTOP the supervisor and every worker for 90 s, then SIGCONT.
#                                  From inside the run that's exactly a sleeping laptop: no code
#                                  runs, the wall clock keeps going, every lease "expires".
#   scripts/chaos.sh kill          kill -9 the supervisor and every worker (then: python3 run.py run)
#   scripts/chaos.sh blackout 120  every fetch fails as if the network were gone (= run.py blackout)
#
# The in-process faults (crash, hang, corrupt, poison, pii_leak, llm_garbage, llm_down, net_drop)
# are enabled with: python3 run.py run --chaos 0.1 [--chaos-faults crash,hang]
set -euo pipefail
cd "$(dirname "$0")/.."
WORKDIR="${WORKDIR:-work}"
PATTERN='^python3 (-u )?run\.py run'   # only the run, never this script or a shell that mentions it

note() {  # leave a trace in the queue, so the morning report shows the drill next to its effect
  python3 -I -c 'import sys; sys.path.insert(0, "."); from factory.queue import TaskQueue
TaskQueue(sys.argv[1] + "/factory.db").event("chaos", sys.argv[2])' "$WORKDIR" "$1"
}

case "${1:-}" in
  freeze)
    secs="${2:-90}"
    note "freeze: supervisor and workers SIGSTOPped for ${secs}s (laptop sleep drill)"
    pkill -STOP -f "$PATTERN"
    echo "frozen for ${secs}s ($(pgrep -fc "$PATTERN") processes)"
    sleep "$secs"
    pkill -CONT -f "$PATTERN"
    echo "thawed"
    ;;
  kill)
    note "kill: kill -9 on the supervisor and every worker (drill)"
    pkill -9 -f "$PATTERN" || true
    echo "killed; resume with: python3 run.py run"
    ;;
  blackout)
    python3 run.py blackout "${2:-120}" --workdir "$WORKDIR"
    ;;
  *)
    sed -n '2,13p' "$0"; exit 2
    ;;
esac
