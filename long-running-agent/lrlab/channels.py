"""Apps and channels: producers of one shape (Inbound) and consumers of one log (events).

Inbound side: CLI, a signed webhook (Slack-style reply, GitHub push, RSS ping) and the scheduler
all build the same `Inbound`. Nothing downstream knows which app a message came from except
through `channel` and `provenance`.

Outbound side: deliveries tail the public event log. Terminal, mail and chat all render the same
conversation because they all read the same rows.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from .state import StateStore


@dataclass(frozen=True)
class Inbound:
    channel: str            # "cli" | "webhook" | "schedule" | ...
    sender: str             # who, as the channel identified them
    thread_key: str         # the channel's notion of "this conversation"
    text: str
    idem_key: str           # same key twice = same message (retries, double cron fires)
    verified: bool          # did the channel authenticate the sender?
    received_at: float = field(default_factory=time.time)


class CliChannel:
    """Local operator at a terminal. Trusted because they already have a shell on the box."""

    def make(self, text: str, thread_key: str, idem_key: str | None = None) -> Inbound:
        key = idem_key or "cli-" + hashlib.sha256(f"{thread_key}|{text}|{time.time()}".encode()).hexdigest()[:16]
        return Inbound("cli", "operator", thread_key, text, key, verified=True)


class WebhookChannel:
    """HMAC-signed JSON webhooks, the Slack/GitHub pattern: signature over timestamp + body,
    reject anything older than five minutes so a captured request can't be replayed tomorrow."""

    TOLERANCE_S = 300

    def __init__(self, secret: str):
        self._secret = secret.encode()

    def sign(self, body: bytes, ts: int | None = None) -> dict[str, str]:
        ts = int(ts if ts is not None else time.time())
        mac = hmac.new(self._secret, f"v1:{ts}:".encode() + body, hashlib.sha256).hexdigest()
        return {"X-Lab-Timestamp": str(ts), "X-Lab-Signature": f"v1={mac}"}

    def parse(self, headers: dict[str, str], body: bytes, now: float | None = None) -> Inbound:
        now = now if now is not None else time.time()
        ts = int(headers.get("X-Lab-Timestamp", "0"))
        expected = self.sign(body, ts)["X-Lab-Signature"]
        ok = hmac.compare_digest(expected, headers.get("X-Lab-Signature", "")) and abs(now - ts) <= self.TOLERANCE_S
        data = json.loads(body)
        return Inbound(
            channel="webhook",
            sender=str(data.get("sender", "unknown")),
            thread_key=str(data["thread"]),
            text=str(data["text"]),
            idem_key="wh-" + str(data["id"]),
            verified=ok,
        )


# ── outbound: deliveries tail the public event log ──────────────────────────────

class Delivery:
    name = "base"

    def render(self, ev: dict) -> None:
        raise NotImplementedError


class TerminalDelivery(Delivery):
    name = "terminal"

    def __init__(self, printer=print):
        self.printer = printer

    def render(self, ev: dict) -> None:
        p = ev["payload"]
        line = f"  [terminal] {p.get('kind', ev['kind'])}: {p.get('text', '')}"
        if p.get("options"):
            line += "  options=" + " | ".join(p["options"])
        if p.get("attachment"):
            line += f"  (+ {p['attachment']['name']}, {len(p['attachment']['content'])} chars)"
        self.printer(line)


class MailboxDelivery(Delivery):
    """Writes one .eml-ish file per message. Stands in for SMTP."""

    name = "mail"

    def __init__(self, outdir: Path):
        self.outdir = outdir
        outdir.mkdir(parents=True, exist_ok=True)

    def render(self, ev: dict) -> None:
        p = ev["payload"]
        body = p.get("text", "")
        if p.get("options"):
            body += "\n\nReply with one of: " + ", ".join(p["options"])
        if p.get("attachment"):
            body += f"\n\n--- {p['attachment']['name']} ---\n{p['attachment']['content']}"
        (self.outdir / f"{ev['seq']:05d}.eml").write_text(
            f"Subject: [agent] {p.get('kind', 'message')}\nX-Conversation: {ev['conv_id']}\n\n{body}\n"
        )


class ChatLogDelivery(Delivery):
    """Appends JSON lines, the shape a Slack/Teams bot would post."""

    name = "chat"

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def render(self, ev: dict) -> None:
        p = ev["payload"]
        with self.path.open("a") as fh:
            fh.write(json.dumps({"conv": ev["conv_id"], "kind": p.get("kind"), "text": p.get("text"),
                                 "options": p.get("options")}) + "\n")


class Dispatcher:
    """At-least-once fan-out: each delivery keeps its own cursor in the state store."""

    def __init__(self, store: StateStore, deliveries: list[Delivery]):
        self.store = store
        self.deliveries = deliveries

    def pump(self) -> int:
        sent = 0
        for d in self.deliveries:
            cur = self.store.cursor(d.name)
            for ev in self.store.events_after(cur, public_only=True):
                if ev["kind"] == "message":
                    d.render(ev)
                    sent += 1
                self.store.set_cursor(d.name, ev["seq"])
        return sent
