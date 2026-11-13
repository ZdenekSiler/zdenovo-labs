"""Same job, same harness, different models (and different ways to build the agent).

    python3 compare.py --model fake --model ollama:llama3.2 --model ollama:granite4.1:3b
    GROQ_API_KEY=... python3 compare.py --model groq:llama-3.3-70b-versatile
    uv run --with anthropic python compare.py --model claude:claude-haiku-5-5 --model claude:claude-sonnet-5-5
    uv run --with claude-agent-sdk python compare.py --approach sdk --model claude-haiku-5-5
    python3 compare.py --approach claude-code --model claude-haiku-5-5

The job is the nightly dependency watch on a subset of the lockfile (default 6 packages chosen to
contain every trap: a 0.x "minor" that is really major, a real major, a pre-release that must be
ignored, an unchanged package). A scripted human answers "pin" to the question.

Graded against ground truth computed from the recorded feeds, not by another model:
  rows_correct   report.md table rows with the right latest version AND change class
  asked          did it ask exactly one question before writing (there are majors in the set)
  delivered      did a final message with the report reach the user
"""
from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import time
from pathlib import Path

from lrlab.channels import CliChannel
from lrlab.config import Budget, LabConfig
from lrlab.fixture_web import FixtureWeb
from lrlab.gateway import Gateway
from lrlab.models import ModelError, make_model
from lrlab.runner_server import Computer
from lrlab.state import StateStore
from lrlab.task import TASK_PROMPT, classify, parse_lockfile, parse_report_table
from lrlab.transcript import Transcript
from lrlab.worker import Worker

LAB = Path(__file__).resolve().parent
SUBSET = ["fastapi", "anthropic", "httpx", "jinja2", "starlette", "pygments"]


def ground_truth(web: FixtureWeb, lock: list[tuple[str, str]], scratch: Path) -> dict[str, dict]:
    comp = Computer(scratch / "gt", web.base, {web.base.split("//")[1]})
    out = {}
    for name, ver in lock:
        latest = comp.pypi_releases(name)["newest_stable"]
        out[name] = {"locked": ver, "latest": latest, "change": classify(ver, latest)}
    return out


def grade(home: Path, store: StateStore, truth: dict) -> dict:
    report = home / "workspace" / "report.md"
    rows = parse_report_table(report.read_text()) if report.exists() else {}
    correct = sum(1 for p, t in truth.items()
                  if rows.get(p, {}).get("latest") == t["latest"] and rows.get(p, {}).get("change") == t["change"])
    msgs = [e["payload"] for e in store.events_after(0, public_only=True)]
    ev = lambda k: store.db.execute("SELECT COUNT(*) FROM events WHERE kind=?", (k,)).fetchone()[0]
    return {
        "rows_correct": f"{correct}/{len(truth)}",
        "asked": sum(1 for m in msgs if m["kind"] == "choice"),
        "delivered": any(m["kind"] == "file" for m in msgs),
        "invalid_calls": ev("tool_invalid"),
        "denied": ev("tool_denied"),
        "done_rejections": ev("done_check_failed"),
        "model_errors": ev("model_error"),
    }


def run_harness(spec: str, home: Path, web: FixtureWeb, max_runs: int, show: bool) -> dict:
    cfg = LabConfig(home=home, pypi_base=web.base, budget=Budget(max_turns=30, max_cost_usd=1.0))
    store = StateStore(cfg.db_path)
    gw = Gateway(store, known_users={"zdenek"})
    thread = "compare/" + spec
    gw.submit(CliChannel().make(TASK_PROMPT.format(fire_time="2026-11-12T01:00:00+00:00"), thread, idem_key="job"))
    conv = store.conversation_for(thread)
    try:
        model = make_model(spec)
    except ModelError as e:
        return {"skipped": str(e)}
    w = Worker(cfg, model)
    log = Transcript(store, verbose=False, printer=lambda s: print("   " + s)) if show else None
    t0 = time.monotonic()
    status = "?"
    try:
        for i in range(max_runs):
            outs = w.run_until_idle(max_runs=1)
            if log:
                log.show()
            if not outs:
                break
            status = outs[-1][1].status
            if status == "parked_human":
                gw.submit(CliChannel().make("pin", thread, idem_key=f"reply-{i}"))   # the scripted human
            elif status != "parked_retry":
                break
            else:
                time.sleep(1)
    finally:
        w.close()
    usage = (store.load_checkpoint(conv) or {}).get("usage", {})
    return {"status": status, "turns": usage.get("turns"), "tokens": usage.get("in", 0) + usage.get("out", 0),
            "cost_usd": None if usage.get("cost_unknown") else round(usage.get("cost", 0.0), 4),
            "wall_s": round(time.monotonic() - t0, 1), **grade(home, store, TRUTH)}


def main() -> None:
    global TRUTH
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", action="append", default=[])
    ap.add_argument("--approach", choices=["harness", "sdk", "claude-code"], default="harness")
    ap.add_argument("--packages", default=",".join(SUBSET))
    ap.add_argument("--max-runs", type=int, default=4)
    ap.add_argument("--show", action="store_true", help="print each transcript")
    ap.add_argument("--out", default=None, help="append results as JSON lines")
    a = ap.parse_args()
    models = a.model or ["fake"]

    full = dict(parse_lockfile((LAB / "lockfile.txt").read_text()))
    lock = [(p, full[p]) for p in a.packages.split(",")]
    scratch = Path(tempfile.mkdtemp(prefix="lrlab-compare-"))
    web = FixtureWeb(LAB / "fixtures" / "pypi").start()
    TRUTH = ground_truth(web, lock, scratch)
    print("ground truth:", {p: f"{t['locked']}->{t['latest']} {t['change']}" for p, t in TRUTH.items()})

    results = []
    for spec in models:
        home = scratch / spec.replace(":", "_").replace("/", "_")
        (home / "workspace").mkdir(parents=True)
        (home / "workspace" / "lockfile.txt").write_text("".join(f"{p}=={v}\n" for p, v in lock))
        print(f"\n## {a.approach} / {spec}")
        if a.approach == "harness":
            r = run_harness(spec, home, web, a.max_runs, a.show)
        elif a.approach == "sdk":
            from approaches.agent_sdk import run_sdk
            r = run_sdk(spec, home, web, TRUTH, grade)
        else:
            from approaches.claude_code import run_claude_code
            r = run_claude_code(spec, home, web, TRUTH, grade)
        r = {"approach": a.approach, "model": spec, **r}
        print("  ", r)
        results.append(r)
        if a.out:
            with open(a.out, "a") as fh:
                fh.write(json.dumps(r) + "\n")

    keys = ["approach", "model", "status", "rows_correct", "asked", "delivered", "turns", "invalid_calls",
            "denied", "done_rejections", "model_errors", "tokens", "cost_usd", "wall_s"]
    print("\n| " + " | ".join(keys) + " |\n|" + "---|" * len(keys))
    for r in results:
        if "skipped" in r:
            print(f"| {r['approach']} | {r['model']} | skipped: {r['skipped']} |")
        else:
            print("| " + " | ".join(str(r.get(k, "")) for k in keys) + " |")
    web.stop()
    shutil.rmtree(scratch, ignore_errors=True)


TRUTH: dict = {}

if __name__ == "__main__":
    main()
