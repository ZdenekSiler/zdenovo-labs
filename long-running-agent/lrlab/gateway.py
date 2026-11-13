"""Entry point: checks and routes input before anything costs money.

Order matters: cheap, local checks first; nothing here calls a model.
  1. authenticated?        (the channel says whether it verified the sender)
  2. small enough?         (an 8 KB cap; a pasted novel is not a task)
  3. under the rate limit? (token bucket per sender)
  4. route                 (thread key -> conversation id)
  5. secret reply?         (if the agent asked for a secret, store it in the vault, keep only a handle)
  6. provenance stamp      (operator / user / untrusted, which the harness's permission policy reads)
  7. enqueue               (deduplicated by idempotency key)
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from .channels import Inbound
from .state import StateStore

MAX_BYTES = 8_000


@dataclass(frozen=True)
class GatewayResult:
    accepted: bool
    reason: str
    conv_id: str | None = None


class TokenBucket:
    def __init__(self, rate_per_s: float, burst: int):
        self.rate, self.burst = rate_per_s, burst
        self._state: dict[str, tuple[float, float]] = {}

    def take(self, key: str, now: float) -> bool:
        tokens, last = self._state.get(key, (float(self.burst), now))
        tokens = min(self.burst, tokens + (now - last) * self.rate)
        if tokens < 1:
            self._state[key] = (tokens, now)
            return False
        self._state[key] = (tokens - 1, now)
        return True


class Gateway:
    def __init__(self, store: StateStore, known_users: set[str], rate_per_min: float = 20, burst: int = 5):
        self.store = store
        self.known_users = known_users
        self.bucket = TokenBucket(rate_per_min / 60.0, burst)

    def provenance(self, msg: Inbound) -> str:
        if msg.channel in ("cli", "schedule"):
            return "operator"
        if msg.sender in self.known_users:
            return "user"
        return "untrusted"

    def submit(self, msg: Inbound) -> GatewayResult:
        if not msg.verified:
            return self._reject(msg, "unauthenticated")
        if len(msg.text.encode()) > MAX_BYTES:
            return self._reject(msg, "too_large")
        if not self.bucket.take(msg.sender, time.time()):
            return self._reject(msg, "rate_limited")

        conv_id = self.store.conversation_for(msg.thread_key)
        text = msg.text
        waiting = self.store.conversation(conv_id)["waiting_for"]
        if waiting and waiting.get("kind") == "secret":
            handle = self.store.vault_put(waiting["secret_name"], conv_id, msg.text.strip())
            text = f"[the user entered the secret; it is stored as {handle}]"
            self.store.set_waiting(conv_id, None)

        prov = self.provenance(msg)
        fresh = self.store.add_inbound(conv_id, msg.idem_key, msg.channel, msg.sender, prov, text)
        if not fresh:
            return GatewayResult(False, "duplicate", conv_id)
        self.store.event(conv_id, "inbound", {"channel": msg.channel, "sender": msg.sender,
                                              "provenance": prov, "chars": len(text)})
        return GatewayResult(True, "queued", conv_id)

    def _reject(self, msg: Inbound, reason: str) -> GatewayResult:
        self.store.event(None, "gateway_reject", {"channel": msg.channel, "sender": msg.sender, "reason": reason})
        return GatewayResult(False, reason)
