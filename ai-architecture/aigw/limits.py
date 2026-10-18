"""Token-aware rate limits and spend budgets, both reserve-then-reconcile.

In-memory for the lab. With more than one gateway replica, the buckets and ledgers must live
in shared storage (Redis + a Lua script for atomic take/refund is the usual answer), or each
replica enforces 1/N of nothing.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from .catalog import Price
from .resilience import Clock
from .types import ChatRequest, ErrorKind, GatewayError, Usage


def estimate_input_tokens(req: ChatRequest) -> int:
    # ~4 chars/token for English. Good enough to *reserve*; never good enough to *bill*.
    # Code, non-Latin scripts and JSON run denser. For exact counts use the provider's
    # token-counting endpoint (Anthropic: POST /v1/messages/count_tokens).
    return sum(len(m.content) for m in req.messages) // 4 + 8 * len(req.messages)


class TokenBucket:
    def __init__(self, clock: Clock, capacity: float, per_second: float):
        self.clock, self.capacity, self.rate = clock, capacity, per_second
        self.level = capacity
        self.updated = clock.now()

    def _refill(self) -> None:
        now = self.clock.now()
        self.level = min(self.capacity, self.level + (now - self.updated) * self.rate)
        self.updated = now

    def take(self, n: float) -> float | None:
        """Take n tokens, or return seconds until n would be available."""
        self._refill()
        if n > self.capacity:
            return float("inf")  # can never fit: reject, don't make the caller wait forever
        if self.level >= n:
            self.level -= n
            return None
        return (n - self.level) / self.rate

    def give_back(self, n: float) -> None:
        self._refill()
        self.level = min(self.capacity, self.level + n)  # n<0 = charge more (debt allowed)


@dataclass(slots=True)
class Reservation:
    key: str
    tokens: int


class TokenRateLimiter:
    """Per-key requests-per-minute AND tokens-per-minute, like the providers enforce on you."""

    def __init__(self, clock: Clock, rpm: int, tpm: int):
        self.clock, self.rpm, self.tpm = clock, rpm, tpm
        self.req_buckets: dict[str, TokenBucket] = {}
        self.tok_buckets: dict[str, TokenBucket] = {}

    def _buckets(self, key: str) -> tuple[TokenBucket, TokenBucket]:
        if key not in self.req_buckets:
            self.req_buckets[key] = TokenBucket(self.clock, self.rpm, self.rpm / 60)
            self.tok_buckets[key] = TokenBucket(self.clock, self.tpm, self.tpm / 60)
        return self.req_buckets[key], self.tok_buckets[key]

    def reserve(self, key: str, req: ChatRequest) -> Reservation:
        rb, tb = self._buckets(key)
        # Worst case: the whole prompt plus every output token max_tokens allows.
        need = estimate_input_tokens(req) + req.max_tokens
        if (wait := rb.take(1)) is not None:
            raise GatewayError(ErrorKind.QUOTA_EXCEEDED, "request rate limit", retry_after=wait)
        if (wait := tb.take(need)) is not None:
            rb.give_back(1)
            raise GatewayError(ErrorKind.QUOTA_EXCEEDED, f"token rate limit (needs {need})",
                               retry_after=None if wait == float("inf") else round(wait, 2))
        return Reservation(key, need)

    def reconcile(self, r: Reservation, actual: Usage | None) -> None:
        used = 0 if actual is None else actual.input_tokens + actual.output_tokens
        self._buckets(r.key)[1].give_back(r.tokens - used)


@dataclass(slots=True)
class Hold:
    id: int
    scopes: tuple[str, ...]
    micro_usd: int


@dataclass
class BudgetLedger:
    """Hierarchical monthly budgets in integer micro-dollars (floats drift; money mustn't).

    A request charges every scope it belongs to (tenant, team, key, feature...), and is refused
    if ANY of them would go over. Holds stop 200 parallel requests from all passing the check
    against the same remaining balance.
    """

    limits: dict[str, int] = field(default_factory=dict)  # scope -> micro-USD per period
    spent: dict[str, int] = field(default_factory=dict)
    held: dict[str, int] = field(default_factory=dict)
    soft_ratio: float = 0.8
    alerts: list[str] = field(default_factory=list)
    _ids: itertools.count = field(default_factory=itertools.count)
    _holds: dict[int, Hold] = field(default_factory=dict)

    def set_limit(self, scope: str, usd: float) -> None:
        self.limits[scope] = int(usd * 1_000_000)

    def hold(self, scopes: tuple[str, ...], usd: float) -> Hold:
        amount = int(usd * 1_000_000) + 1
        for s in scopes:
            limit = self.limits.get(s)
            if limit is None:
                continue
            if self.spent.get(s, 0) + self.held.get(s, 0) + amount > limit:
                raise GatewayError(ErrorKind.BUDGET_EXCEEDED, f"budget exhausted for {s}")
        h = Hold(next(self._ids), scopes, amount)
        for s in scopes:
            self.held[s] = self.held.get(s, 0) + amount
        self._holds[h.id] = h
        return h

    def settle(self, h: Hold, usd: float) -> None:
        h = self._holds.pop(h.id)
        actual = int(round(usd * 1_000_000))
        for s in h.scopes:
            self.held[s] -= h.micro_usd
            before = self.spent.get(s, 0)
            self.spent[s] = before + actual
            limit = self.limits.get(s)
            if limit and before < self.soft_ratio * limit <= self.spent[s]:
                self.alerts.append(f"{s} crossed {self.soft_ratio:.0%} of budget")

    def spent_usd(self, scope: str) -> float:
        return self.spent.get(scope, 0) / 1_000_000


def worst_case_cost(price: Price, req: ChatRequest) -> float:
    return (estimate_input_tokens(req) * price.input + req.max_tokens * price.output) / 1_000_000
