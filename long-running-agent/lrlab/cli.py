"""Command line entry points, the things cron, systemd and GitHub Actions call.

  python -m lrlab.cli init     --home DIR [--live-pypi]      create state, workspace, the 01:00 schedule
  python -m lrlab.cli tick     --home DIR [--model SPEC]      scheduler tick + work until idle (cron runs this)
  python -m lrlab.cli fire     --home DIR                     run the job now (manual trigger)
  python -m lrlab.cli reply    --home DIR --thread T TEXT     answer the agent from a terminal
  python -m lrlab.cli serve    --home DIR [--port 8787]       signed webhook (POST /hook) + live view (GET /)
  python -m lrlab.cli status   --home DIR                     conversations and their states
"""
from __future__ import annotations

import argparse
import json
import shutil
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .channels import ChatLogDelivery, CliChannel, Dispatcher, Inbound, MailboxDelivery, TerminalDelivery, WebhookChannel
from .config import LabConfig
from .gateway import Gateway
from .live import LiveServer
from .models import make_model
from .scheduler import Scheduler
from .state import StateStore
from .task import TASK_PROMPT
from .worker import Worker

LAB = Path(__file__).resolve().parent.parent


def config(home: Path) -> LabConfig:
    meta = json.loads((home / "deployment.json").read_text())
    return LabConfig(home=home, pypi_base=meta["pypi_base"], webhook_secret=meta["webhook_secret"])


def gateway_for(store: StateStore) -> Gateway:
    return Gateway(store, known_users={"zdenek"})


def dispatch(cfg: LabConfig, store: StateStore) -> int:
    return Dispatcher(store, [TerminalDelivery(), MailboxDelivery(cfg.outbox / "mail"),
                              ChatLogDelivery(cfg.outbox / "chat.jsonl")]).pump()


def cmd_init(a) -> None:
    home = Path(a.home)
    (home / "workspace").mkdir(parents=True, exist_ok=True)
    shutil.copy(LAB / "lockfile.txt", home / "workspace" / "lockfile.txt")
    meta = {"pypi_base": "https://pypi.org" if a.live_pypi else a.pypi_base,
            "webhook_secret": a.webhook_secret}
    (home / "deployment.json").write_text(json.dumps(meta, indent=1))
    store = StateStore(home / "state.db")
    Scheduler(store, gateway_for(store)).add_daily(
        "nightly-deps", "01:00", "nightly-deps/{date}", TASK_PROMPT, catchup="once", jitter_s=600)
    print(f"initialised {home} (pypi: {meta['pypi_base']})")


def cmd_tick(a) -> None:
    cfg = config(Path(a.home))
    store = StateStore(cfg.db_path)
    fired = Scheduler(store, gateway_for(store)).tick(datetime.now(timezone.utc))
    for f in fired:
        print("fired:", f)
    w = Worker(cfg, make_model(a.model))
    try:
        for conv, out in w.run_until_idle():
            print(f"{conv}: {out.status} ({out.reason}) model_calls={out.model_calls} tool_calls={out.tool_calls}")
    finally:
        w.close()
    dispatch(cfg, store)


def cmd_fire(a) -> None:
    cfg = config(Path(a.home))
    store = StateStore(cfg.db_path)
    now = datetime.now(timezone.utc)
    msg = CliChannel().make(TASK_PROMPT.format(fire_time=now.isoformat()), f"nightly-deps/manual-{now:%Y%m%dT%H%M%S}")
    print(gateway_for(store).submit(msg))
    cmd_tick(a)


def cmd_reply(a) -> None:
    cfg = config(Path(a.home))
    store = StateStore(cfg.db_path)
    print(gateway_for(store).submit(CliChannel().make(" ".join(a.text), a.thread)))
    cmd_tick(a)


def cmd_status(a) -> None:
    store = StateStore(Path(a.home) / "state.db")
    for r in store.db.execute("SELECT thread_key, status, fence, waiting_for FROM conversations ORDER BY created_at"):
        print(dict(r))


def cmd_serve(a) -> None:
    cfg = config(Path(a.home))
    store = StateStore(cfg.db_path)
    gw, hook = gateway_for(store), WebhookChannel(cfg.webhook_secret)
    live = LiveServer(cfg.db_path, a.port + 1).start()

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            res = gw.submit(hook.parse(dict(self.headers), body))
            self.send_response(202 if res.accepted else 400)
            self.end_headers()
            self.wfile.write(json.dumps(res.__dict__).encode())

    print(f"webhook on http://127.0.0.1:{a.port}/hook, live view on http://127.0.0.1:{live.port}/?conv=<id>")
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), H)
    try:
        srv.serve_forever()
    finally:
        live.stop()


def main() -> None:
    ap = argparse.ArgumentParser(prog="lrlab")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("init", "tick", "fire", "reply", "serve", "status"):
        p = sub.add_parser(name)
        p.add_argument("--home", required=True)
        p.add_argument("--model", default="fake")
    sub.choices["init"].add_argument("--live-pypi", action="store_true")
    sub.choices["init"].add_argument("--pypi-base", default="https://pypi.org")
    sub.choices["init"].add_argument("--webhook-secret", default="change-me-" + str(int(time.time())))
    sub.choices["reply"].add_argument("--thread", required=True)
    sub.choices["reply"].add_argument("text", nargs="+")
    sub.choices["serve"].add_argument("--port", type=int, default=8787)
    a = ap.parse_args()
    {"init": cmd_init, "tick": cmd_tick, "fire": cmd_fire, "reply": cmd_reply,
     "serve": cmd_serve, "status": cmd_status}[a.cmd](a)


if __name__ == "__main__":
    main()
