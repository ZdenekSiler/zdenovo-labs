"""Break it on purpose. Each drill gets a fresh deployment and the same recorded PyPI.

    python3 chaos.py                 # all drills
    python3 chaos.py kill model      # pick some

  kill      SIGKILL the worker mid-run; a second worker resumes from the checkpoint
  zombie    the killed worker comes back and tries to write: the fence stops it
  model     the provider returns 429s: backoff, then park, then resume
  network   the "internet" goes down mid-run: park, then resume when it is back
  hang      a tool hangs: the computer process is killed and restarted, the run parks
  budget    the turn budget runs out: the harness tells the human and waits
  done      the model claims "final" too early: the done check refuses to deliver it
  secret    the agent asks for a token: it reaches the vault, never the transcript
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

from lrlab.channels import CliChannel
from lrlab.config import Budget, LabConfig
from lrlab.fixture_web import FixtureWeb
from lrlab.gateway import Gateway
from lrlab.models.base import ModelError, ModelReply, ToolCall
from lrlab.models.fake import ScriptedResearcher
from lrlab.state import LeaseLost, StateStore
from lrlab.task import TASK_PROMPT
from lrlab.tools import default_tools
from lrlab.transcript import Transcript
from lrlab.worker import Worker

LAB = Path(__file__).resolve().parent
SCRATCH = Path(tempfile.mkdtemp(prefix="lrlab-chaos-"))


class Drill:
    def __init__(self, name: str, web: FixtureWeb, **cfg):
        self.home = SCRATCH / name
        self.cfg = LabConfig(home=self.home, pypi_base=web.base, model_retry_base_s=0.01, **cfg)
        self.cfg.workspace.mkdir(parents=True, exist_ok=True)
        shutil.copy(LAB / "lockfile.txt", self.cfg.workspace / "lockfile.txt")
        self.store = StateStore(self.cfg.db_path)
        self.gw = Gateway(self.store, known_users={"zdenek"})
        self.log = Transcript(self.store, verbose=False, printer=lambda s: print("   " + s))
        self.thread = f"drill/{name}"
        self.conv = self.store.conversation_for(self.thread)

    def start_job(self) -> None:
        self.gw.submit(CliChannel().make(TASK_PROMPT.format(fire_time="drill"), self.thread, idem_key="job"))

    def reply(self, text: str, key: str) -> None:
        self.gw.submit(CliChannel().make(text, self.thread, idem_key=key))

    def run(self, worker: Worker) -> None:
        for _, out in worker.run_until_idle():
            print(f"   => {out.status}: {out.reason} (model_calls={out.model_calls}, tool_calls={out.tool_calls})")
        self.log.show()


def header(title: str) -> None:
    print("\n" + "=" * 78 + f"\nDRILL  {title}\n" + "=" * 78)


# ── 1. kill -9 mid-run, resume on another worker ─────────────────────────────────
def drill_kill(web: FixtureWeb) -> None:
    header("kill: SIGKILL the worker in the middle of the PyPI lookups")
    d = Drill("kill", web, lease_s=2.0)
    d.start_job()
    child = subprocess.Popen([sys.executable, __file__, "_child", str(d.home), web.base],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    while d.store.db.execute("SELECT COUNT(*) FROM tool_journal WHERE tool='pypi_releases'").fetchone()[0] < 6:
        time.sleep(0.05)
    os.kill(child.pid, signal.SIGKILL)
    child.wait()
    done = d.store.db.execute("SELECT COUNT(*) FROM tool_journal WHERE tool='pypi_releases'").fetchone()[0]
    cp = d.store.load_checkpoint(d.conv)
    print(f"   worker-a killed (SIGKILL) after {done} lookups; checkpoint holds {len(cp['messages'])} messages; "
          f"conversation status={d.store.conversation(d.conv)['status']!r}")
    hits_before = dict(web.feed_hits())
    print("   waiting for worker-a's lease to expire (2 s)...")
    time.sleep(2.2)
    w = Worker(d.cfg, ScriptedResearcher(), owner="worker-b")
    d.log.seq = d.store.db.execute("SELECT MAX(seq) FROM events").fetchone()[0]
    d.log.verbose = True
    d.run(w)
    w.close()
    refetched = {p: n - hits_before.get(p, 0) for p, n in web.feed_hits().items() if n - hits_before.get(p, 0) > 0}
    print(f"   feeds fetched by worker-b: {len(refetched)} of 16; "
          f"fetched by both workers: {[p for p in refetched if hits_before.get(p)] or 'none'}")


def child_worker(home: str, base: str) -> None:
    cfg = LabConfig(home=Path(home), pypi_base=base, lease_s=2.0)
    Worker(cfg, ScriptedResearcher(batch=1), owner="worker-a", slow_tools_s=0.3).run_until_idle()


# ── 2. the zombie: an old lease holder tries to write after a takeover ───────────
def drill_zombie(web: FixtureWeb) -> None:
    header("zombie: a paused worker wakes up after someone else took over")
    d = Drill("zombie", web, lease_s=0.5)
    d.start_job()
    a = Worker(d.cfg, ScriptedResearcher(), owner="worker-a")
    lease_a = a.queue.claim()
    print(f"   worker-a claims: fence={lease_a.fence}; then stalls (GC pause, laptop lid, SIGSTOP)...")
    time.sleep(0.6)
    b = Worker(d.cfg, ScriptedResearcher(), owner="worker-b")
    lease_b = b.queue.claim()
    print(f"   worker-b claims after the lease expired: fence={lease_b.fence}")
    try:
        d.store.save_checkpoint(d.conv, lease_a.fence, {"messages": ["stale write from worker-a"]})
        print("   !! stale write accepted (this would be a bug)")
    except LeaseLost as e:
        print(f"   worker-a's checkpoint write is refused: {e}")
    print(f"   worker-a renew -> {a.queue.renew(lease_a)}, worker-b renew -> {b.queue.renew(lease_b)}")
    a.close()
    b.close()


# ── 3. model provider rate limits ────────────────────────────────────────────────
class Flaky:
    """Wraps a model; fails the first `n` calls with a 429, or every call if n < 0."""

    def __init__(self, inner, n: int):
        self.inner, self.n, self.name, self.seen = inner, n, inner.name + "+429s", 0

    def complete(self, system, messages, tools):
        self.seen += 1
        if self.n < 0 or self.seen <= self.n:
            raise ModelError("HTTP 429: rate limit exceeded", transient=True)
        return self.inner.complete(system, messages, tools)


def drill_model(web: FixtureWeb) -> None:
    header("model: two 429s, then a provider outage, then recovery")
    d = Drill("model", web)
    d.start_job()
    delays: list[float] = []
    w = Worker(d.cfg, Flaky(ScriptedResearcher(), 2), sleep=delays.append)
    w.harness.park_retry_s = 0.3
    out = w.run_once()[1]
    d.log.show()
    print(f"   backoff sleeps: {delays}  => {out.status}")
    w.harness.model = Flaky(ScriptedResearcher(), -1)
    d.reply("pin", key="r1")                                 # the answer arrives, the provider is down
    out = w.run_once()[1]
    d.log.show()
    print(f"   all retries failed => {out.status}, retry in {out.retry_at - time.time():.1f}s "
          f"(conversation status {d.store.conversation(d.conv)['status']!r})")
    print(f"   claim before retry_at -> {w.queue.claim()}")
    time.sleep(0.35)
    w.harness.model = ScriptedResearcher()
    d.run(w)
    w.close()


# ── 4. network outage ────────────────────────────────────────────────────────────
def drill_network(web: FixtureWeb) -> None:
    header("network: PyPI goes away mid-run, comes back later")
    d = Drill("network", web)
    d.start_job()
    w = Worker(d.cfg, ScriptedResearcher())
    w.harness.park_retry_s = 0.2
    port = web.port
    orig = w.runner.call
    calls = {"n": 0}

    def outage_after_five(tool, args, timeout_s=30.0, scope=None):
        if tool == "pypi_releases":
            calls["n"] += 1
            if calls["n"] == 6:
                web.stop()
                print(f"   [chaos] fixture PyPI on port {port} is DOWN")
        return orig(tool, args, timeout_s, scope)

    w.runner.call = outage_after_five
    d.run(w)
    print(f"   conversation status: {d.store.conversation(d.conv)['status']!r}")
    web.port = port
    web.start()
    print(f"   [chaos] fixture PyPI is back on port {web.port}")
    time.sleep(0.25)
    d.run(w)
    w.close()


# ── 5. a hung tool ───────────────────────────────────────────────────────────────
def drill_hang(web: FixtureWeb) -> None:
    header("hang: a lookup takes forever; the computer is killed, not the conversation")
    d = Drill("hang", web)
    d.start_job()
    tools = default_tools()
    tools.add(replace(tools.get("pypi_releases"), timeout_s=0.5))
    w = Worker(d.cfg, ScriptedResearcher(), tools=tools, slow_tools_s=2.0)
    w.harness.park_retry_s = 0.1
    out = w.run_once()[1]
    d.log.show()
    print(f"   => {out.status}; computer restarts so far: {w.runner.restarts}")
    w.close()


# ── 6. budget exhaustion ─────────────────────────────────────────────────────────
def drill_budget(web: FixtureWeb) -> None:
    header("budget: three model turns allowed; the job needs eight")
    d = Drill("budget", web, budget=Budget(max_turns=3))
    d.start_job()
    w = Worker(d.cfg, ScriptedResearcher())
    d.run(w)
    print("   the human replies 'continue' (a new budget of 3 turns)...")
    d.reply("continue", key="c1")
    d.run(w)
    w.close()


# ── 7. premature "done" ──────────────────────────────────────────────────────────
def drill_done(web: FixtureWeb) -> None:
    header("done: the model says final before doing the work")
    d = Drill("done", web)
    d.start_job()
    w = Worker(d.cfg, ScriptedResearcher(lazy_finish=True))
    d.run(w)
    print(f"   messages the user actually received: "
          f"{[e['payload']['kind'] for e in d.store.events_after(0, d.conv)]}")
    w.close()


# ── 8. secure secret entry ───────────────────────────────────────────────────────
class AsksForToken:
    name = "fake:asks-for-token"

    def complete(self, system, messages, tools):
        if not any(m["role"] == "tool" for m in messages):
            return ModelReply("I need a token to open the upgrade PR.", [ToolCall(
                "s1", "send_to_user", {"kind": "secret", "secret_name": "github_token",
                                       "text": "Paste a fine-grained GitHub token with contents:write on zdenovo."})])
        last_user = [m for m in messages if m["role"] == "user"][-1]["content"]
        return ModelReply(f"Got it: {last_user.splitlines()[-1]}", [])


def drill_secret(web: FixtureWeb) -> None:
    header("secret: the token goes to the vault; the model only ever sees a handle")
    d = Drill("secret", web)
    d.start_job()
    w = Worker(d.cfg, AsksForToken())
    w.harness.quiet_check = None          # this toy agent has no report to deliver
    d.run(w)
    token = "ghp_" + "x" * 36
    print(f"   the user pastes {token[:8]}… into the secure prompt")
    d.reply(token, key="tok")
    d.run(w)
    blob = json.dumps(d.store.load_checkpoint(d.conv)) + json.dumps(
        [e["payload"] for e in d.store.events_after(0, public_only=False)])
    print(f"   token in checkpoint or event log: {token in blob}; "
          f"in vault: {d.store.vault_get('secret://github_token') == token}")
    w.close()


DRILLS = {"kill": drill_kill, "zombie": drill_zombie, "model": drill_model, "network": drill_network,
          "hang": drill_hang, "budget": drill_budget, "done": drill_done, "secret": drill_secret}


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "_child":
        child_worker(sys.argv[2], sys.argv[3])
        return
    picked = sys.argv[1:] or list(DRILLS)
    web = FixtureWeb(LAB / "fixtures" / "pypi").start()
    try:
        for name in picked:
            DRILLS[name](web)
    finally:
        web.stop()
        shutil.rmtree(SCRATCH, ignore_errors=True)


if __name__ == "__main__":
    main()
