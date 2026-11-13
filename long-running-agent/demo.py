"""The overnight run, end to end, offline and deterministic.

    python3 demo.py                      # scripted model, recorded PyPI feeds, no network, no keys
    python3 demo.py --model ollama:llama3.2
    uv run --with anthropic python demo.py --model claude:claude-haiku-5-5

Scenes:
  1. 01:00  the scheduler fires (twice, the second one is deduplicated)
  2. night  the agent works, hits a policy wall, asks one question, parks
  3. 07:40  a forged webhook is rejected; the real signed reply resumes the run; report delivered
  4. +1d    nothing changed: the agent stays quiet
  5. +4d    the laptop slept three nights: catch-up policy "once" runs one job, not three
  6. live   the SSE view replays the same conversation every app shows
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from lrlab.channels import ChatLogDelivery, Dispatcher, MailboxDelivery, TerminalDelivery, WebhookChannel
from lrlab.config import LabConfig
from lrlab.fixture_web import FixtureWeb
from lrlab.gateway import Gateway
from lrlab.live import LiveServer
from lrlab.models import make_model
from lrlab.scheduler import Scheduler
from lrlab.state import StateStore
from lrlab.task import TASK_PROMPT
from lrlab.transcript import Transcript
from lrlab.worker import Worker

LAB = Path(__file__).resolve().parent
UTC = timezone.utc


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="fake")
    ap.add_argument("--home", default=None, help="keep state here instead of a temp dir")
    a = ap.parse_args()

    home = Path(a.home) if a.home else Path(tempfile.mkdtemp(prefix="lrlab-demo-"))
    if home.exists() and a.home:
        shutil.rmtree(home)
    web = FixtureWeb(LAB / "fixtures" / "pypi").start()
    cfg = LabConfig(home=home, pypi_base=web.base, webhook_secret="demo-secret")
    cfg.workspace.mkdir(parents=True, exist_ok=True)
    shutil.copy(LAB / "lockfile.txt", cfg.workspace / "lockfile.txt")

    store = StateStore(cfg.db_path)
    gw = Gateway(store, known_users={"zdenek"})
    sched = Scheduler(store, gw)
    hook = WebhookChannel(cfg.webhook_secret)
    log = Transcript(store)
    deliveries = Dispatcher(store, [TerminalDelivery(), MailboxDelivery(cfg.outbox / "mail"),
                                    ChatLogDelivery(cfg.outbox / "chat.jsonl")])
    model = make_model(a.model)
    worker = Worker(cfg, model, owner="worker-a")

    day1 = datetime(2026, 11, 12, tzinfo=UTC)
    sched.add_daily("nightly-deps", "01:00", "nightly-deps/{date}", TASK_PROMPT,
                    catchup="once", jitter_s=0, created=day1)

    print("=" * 78)
    print("SCENE 1  01:00 UTC: the schedule fires (cron calls `tick` every 5 minutes)")
    print("=" * 78)
    for t in (day1.replace(hour=0, minute=55), day1.replace(hour=1, minute=0, second=30)):
        print(f"  tick {t:%H:%M:%S} -> {sched.tick(t) or 'nothing due'}")
    # a second cron host (or a retried cron job) fires the same slot again
    print("  second host fires the same slot ->", gw.submit(
        __import__("lrlab.channels", fromlist=["Inbound"]).Inbound(
            "schedule", "schedule:nightly-deps", "nightly-deps/2026-11-12",
            TASK_PROMPT.format(fire_time="2026-11-12T01:00:00+00:00"),
            "nightly-deps@2026-11-12T01:00:00+00:00", True)).reason)
    log.show()

    print("\n" + "=" * 78)
    print("SCENE 2  night: the worker claims the conversation and runs the harness")
    print("=" * 78)
    for conv, out in worker.run_until_idle():
        print(f"  [{conv}] {out.status}: model_calls={out.model_calls} tool_calls={out.tool_calls}")
    log.show()
    print("  deliveries (every app renders the same public event):")
    deliveries.pump()
    conv = store.conversation_for("nightly-deps/2026-11-12")
    print("  conversation state:", {k: store.conversation(conv)[k] for k in ("status", "waiting_for", "fence")})

    print("\n" + "=" * 78)
    print("SCENE 3  07:40: replies arrive through the signed webhook")
    print("=" * 78)
    body = json.dumps({"id": "slack-evt-1", "thread": "nightly-deps/2026-11-12",
                       "sender": "zdenek", "text": "upgrade-branch"}).encode()
    forged = {"X-Lab-Timestamp": str(int(datetime.now(UTC).timestamp())), "X-Lab-Signature": "v1=deadbeef"}
    print("  forged signature  ->", gw.submit(hook.parse(forged, body)).reason)
    stale = hook.sign(body, ts=int(datetime.now(UTC).timestamp()) - 3600)
    print("  replayed (1h old) ->", gw.submit(hook.parse(stale, body)).reason)
    print("  real reply        ->", gw.submit(hook.parse(hook.sign(body), body)).reason)
    print("  same reply again  ->", gw.submit(hook.parse(hook.sign(body), body)).reason)
    calls_before = getattr(model, "calls", None)
    for conv_id, out in worker.run_until_idle():
        print(f"  [{conv_id}] {out.status}: model_calls={out.model_calls} tool_calls={out.tool_calls}")
    log.show()
    deliveries.pump()
    if calls_before is not None:
        print(f"  model calls this run: {model.calls - calls_before} "
              "(the final send_to_user ended the run; no extra model call to say 'done')")
    print("\n  report.md:")
    print("    " + (cfg.workspace / "report.md").read_text().replace("\n", "\n    "))

    print("=" * 78)
    print("SCENE 4  next night, 01:00: nothing new on PyPI")
    print("=" * 78)
    msgs_before = len(store.events_after(0, public_only=True))
    print("  tick ->", sched.tick(day1 + timedelta(days=1, hours=1, minutes=2)))
    for conv_id, out in worker.run_until_idle():
        print(f"  [{conv_id}] {out.status}: model_calls={out.model_calls} tool_calls={out.tool_calls}")
    log.verbose = False
    log.show()
    print(f"  messages sent to the user tonight: {len(store.events_after(0, public_only=True)) - msgs_before}")

    print("\n" + "=" * 78)
    print("SCENE 5  the laptop slept through three nights; it wakes at 08:00 on day 5")
    print("=" * 78)
    print("  tick ->", sched.tick(day1 + timedelta(days=4, hours=8)))
    for conv_id, out in worker.run_until_idle():
        print(f"  [{conv_id}] {out.status}: model_calls={out.model_calls} tool_calls={out.tool_calls}")
    log.show()

    print("\n" + "=" * 78)
    print("SCENE 6  live view: the same conversation over SSE")
    print("=" * 78)
    live = LiveServer(cfg.db_path).start()
    with urllib.request.urlopen(f"http://127.0.0.1:{live.port}/events?conv={conv}&once=1", timeout=5) as r:
        frames = [json.loads(l[6:]) for l in r.read().decode().splitlines() if l.startswith("data: ")]
    for f in frames:
        print(f"  sse: [{f['kind']}] {f['text'][:90]}")
    live.stop()

    mail = sorted((cfg.outbox / "mail").glob("*.eml"))
    chat = (cfg.outbox / "chat.jsonl").read_text().splitlines()
    print("\nTOTALS")
    print(f"  conversations: {store.db.execute('SELECT COUNT(*) FROM conversations').fetchone()[0]}, "
          f"statuses: {[r[0] for r in store.db.execute('SELECT status FROM conversations ORDER BY created_at')]}")
    print(f"  feed fetches per package (per-night cache + journal => one fetch per package per night run): "
          f"{sorted(set(web.feed_hits().values()))} over {len(web.feed_hits())} packages")
    print(f"  robots.txt fetches: {web.hits['/robots.txt']}")
    print(f"  redactions applied: {worker.harness.redactor.count}")
    print(f"  mail files: {len(mail)}, chat lines: {len(chat)}")
    worker.close()
    web.stop()
    if not a.home:
        shutil.rmtree(home)


if __name__ == "__main__":
    sys.exit(main())
