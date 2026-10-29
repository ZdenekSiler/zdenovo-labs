#!/usr/bin/env python3
"""Dark factory lab: an unattended, crash-tolerant pipeline that joins Czech flat listings
(bezrealitky.cz, politely, robots.txt enforced) with ~3M RÚIAN address points and ČSÚ census data,
reads seller signals out of the ads with a local Ollama model, and leaves you a morning report.

  python3 run.py run [--max-per-offer N] [--workers N] [--chaos 0.1] [--model llama3.2]
  python3 run.py status          # queue counts per stage
  python3 run.py report          # (re)write work/out/MORNING_REPORT.md and print it
  python3 run.py requeue <id>    # give a dead task another go (after fixing its cause)
  python3 run.py run --escalate-model llama3   # attempts 3+ of an LLM task use the bigger model
  python3 run.py stop            # kill switch: workers finish their current task and exit
  python3 run.py blackout 120    # chaos drill: every fetch fails as if offline for 120 s
  python3 run.py chaos           # list the chaos faults (enable with --chaos 0.1 [--chaos-faults a,b])
"""
import argparse
import sys
import time
from pathlib import Path

from factory.report import MorningReport
from factory.stages import PIPELINE
from factory.supervisor import Supervisor
from factory.workspace import FAULTS, Workspace


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["run", "status", "report", "requeue", "stop", "blackout", "chaos"])
    ap.add_argument("arg", nargs="?", help="task id for requeue, seconds for blackout")
    ap.add_argument("--workdir", default="work")
    ap.add_argument("--offers", help="prodej,pronajem (default: both)")
    ap.add_argument("--max-per-offer", type=int, help="newest N listings per offer (default: all)")
    ap.add_argument("--net-workers", type=int)
    ap.add_argument("--workers", type=int)
    ap.add_argument("--llm-workers", type=int)
    ap.add_argument("--chaos", type=float)
    ap.add_argument("--chaos-faults", help="comma-separated subset of: run.py chaos")
    ap.add_argument("--model")
    ap.add_argument("--escalate-model")
    ap.add_argument("--deadline-min", type=float)
    a = ap.parse_args()

    overrides = {"offers": a.offers, "max_per_offer": a.max_per_offer, "net_workers": a.net_workers,
                 "workers": a.workers, "llm_workers": a.llm_workers,
                 "chaos": a.chaos, "model": a.model, "escalate_model": a.escalate_model, "chaos_faults": a.chaos_faults, "deadline_min": a.deadline_min}
    # Only `run` pins the config: `blackout` or `status` on a fresh workdir must not lock in defaults.
    ws = Workspace.open(Path(a.workdir), overrides, pin=a.command == "run")

    if a.command == "run":
        t0 = time.time()
        ok = Supervisor(ws, log=lambda m: print(f"{time.strftime('%H:%M:%S')} {m}", flush=True)).run()
        print(f"\nwall time {time.time() - t0:,.0f}s\n")
        print(MorningReport(ws).write())
        return 0 if ok else 1
    if a.command == "status":
        for s in PIPELINE:
            print(f"{s.name:10} {ws.queue.counts(s.name)}")
        return 0
    if a.command == "report":
        print(MorningReport(ws).write())
        return 0
    if a.command == "requeue":
        if not a.arg:
            ap.error("requeue needs a task id")
        ok = ws.queue.requeue(a.arg)
        print("requeued" if ok else f"{a.arg} is not dead (or doesn't exist)")
        return 0 if ok else 1
    if a.command == "chaos":
        for name, what in FAULTS.items():
            print(f"{name:12} {what}")
        print("\ndrills (outside the workers): run.py blackout N, scripts/chaos.sh freeze N, scripts/chaos.sh kill")
        return 0
    if a.command == "blackout":
        seconds = float(a.arg or 120)
        (ws.root / "BLACKOUT").write_text(str(time.time() + seconds))
        ws.queue.event("chaos", f"blackout: every fetch offline for {seconds:.0f}s (drill)")
        print(f"network 'down' for {seconds:.0f}s: watch the stage pause, then resume on its own")
        return 0
    if a.command == "stop":
        ws.stop_file.touch()
        print(f"touched {ws.stop_file}; the run halts after in-flight tasks")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
