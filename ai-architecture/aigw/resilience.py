"""Deadlines, retries, retry budgets, circuit breakers, fallback chains and hedging."""

from __future__ import annotations

import asyncio
import enum
import random
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import contextmanager
from dataclasses import dataclass, field, replace

from .catalog import Deployment
from .providers.base import Provider
from .types import FALLBACKABLE, ChatRequest, ChatResponse, ErrorKind, GatewayError, StreamChunk


class Clock:
    def now(self) -> float:
        return time.monotonic()

    async def sleep(self, s: float) -> None:
        await asyncio.sleep(s)


class FakeClock(Clock):
    """Time only moves when someone sleeps. Makes backoff and breaker demos reproducible."""

    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.t += s

    def advance(self, s: float) -> None:
        self.t += s


@dataclass(slots=True)
class Deadline:
    """One budget for the whole request, shared by every retry and fallback inside it."""

    clock: Clock
    expires_at: float

    @classmethod
    def after(cls, clock: Clock, seconds: float) -> Deadline:
        return cls(clock, clock.now() + seconds)

    def remaining(self) -> float:
        return max(0.0, self.expires_at - self.clock.now())

    def attempt_timeout(self, cap: float) -> float:
        left = self.remaining()
        if left <= 0:
            raise GatewayError(ErrorKind.DEADLINE, "request deadline exceeded")
        return min(cap, left)


@dataclass(slots=True)
class RetryPolicy:
    max_attempts: int = 3  # per deployment, including the first try
    base: float = 0.25
    cap: float = 4.0
    rng: random.Random = field(default_factory=lambda: random.Random(7))

    def backoff(self, attempt: int, retry_after: float | None) -> float:
        # "Full jitter": uniform(0, min(cap, base * 2^n)). Spreads synchronized clients apart.
        delay = self.rng.uniform(0, min(self.cap, self.base * 2 ** attempt))
        return max(delay, retry_after or 0.0)


class RetryBudget:
    """Retries may add at most `ratio` extra load (gRPC/Finagle-style retry throttling).

    Each first attempt deposits `ratio` tokens; each retry spends one. When a provider is
    down, retries stop amplifying the outage after the small reserve is spent.
    """

    def __init__(self, ratio: float = 0.1, reserve: float = 10.0):
        self.ratio, self.cap = ratio, reserve
        self.tokens = reserve

    def on_request(self) -> None:
        self.tokens = min(self.cap, self.tokens + self.ratio)

    def try_spend(self) -> bool:
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


class BreakerState(enum.StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Rolling-window breaker: opens on failure *rate*, not on a streak, with a min volume."""

    def __init__(self, clock: Clock, window: int = 20, min_calls: int = 5,
                 failure_rate: float = 0.5, cooldown: float = 30.0):
        self.clock, self.window, self.min_calls = clock, window, min_calls
        self.failure_rate, self.cooldown = failure_rate, cooldown
        self.results: deque[bool] = deque(maxlen=window)
        self.state = BreakerState.CLOSED
        self.opened_at = 0.0
        self.probe_in_flight = False
        self.transitions: list[str] = []

    def _go(self, state: BreakerState) -> None:
        if state != self.state:
            self.transitions.append(f"{self.state}->{state}@{self.clock.now():.1f}s")
            self.state = state

    def allow(self) -> bool:
        if self.state == BreakerState.OPEN and self.clock.now() - self.opened_at >= self.cooldown:
            self._go(BreakerState.HALF_OPEN)
        if self.state == BreakerState.CLOSED:
            return True
        if self.state == BreakerState.HALF_OPEN and not self.probe_in_flight:
            self.probe_in_flight = True  # exactly one probe; everyone else keeps failing over
            return True
        return False

    def record(self, ok: bool) -> None:
        if self.state == BreakerState.HALF_OPEN:
            self.probe_in_flight = False
            if ok:
                self.results.clear()
                self._go(BreakerState.CLOSED)
            else:
                self.opened_at = self.clock.now()
                self._go(BreakerState.OPEN)
            return
        self.results.append(ok)
        failures = self.results.count(False)
        if len(self.results) >= self.min_calls and failures / len(self.results) >= self.failure_rate:
            self.opened_at = self.clock.now()
            self._go(BreakerState.OPEN)


@dataclass(frozen=True, slots=True)
class Attempt:
    provider: str
    model: str
    n: int
    outcome: str  # "ok" or an ErrorKind value
    slept: float = 0.0


class _NoopRecorder:
    def first_chunk(self) -> None: ...
    def success(self, resp, usage=None, finish=None) -> None: ...


@contextmanager
def no_instrument(d, req, n):
    yield _NoopRecorder()


class ResilientRouter:
    """Walks a fallback chain, retrying each deployment within one shared deadline."""

    def __init__(self, providers: dict[str, Provider], clock: Clock | None = None,
                 policy: RetryPolicy | None = None, budget: RetryBudget | None = None,
                 attempt_timeout: float = 30.0, breaker_factory=None, instrument=no_instrument):
        self.providers = providers
        self.instrument = instrument  # (deployment, request, attempt) -> span recorder
        self.clock = clock or Clock()
        self.policy = policy or RetryPolicy()
        self.budget = budget or RetryBudget()
        self.attempt_timeout = attempt_timeout
        self._breaker_factory = breaker_factory or (lambda: CircuitBreaker(self.clock))
        self.breakers: dict[str, CircuitBreaker] = {}

    def breaker(self, d: Deployment) -> CircuitBreaker:
        key = f"{d.provider}/{d.model}"
        if key not in self.breakers:
            self.breakers[key] = self._breaker_factory()
        return self.breakers[key]

    async def _call(self, d: Deployment, req: ChatRequest, timeout: float, n: int) -> ChatResponse:
        provider = self.providers[d.provider]
        with self.instrument(d, req, n) as rec:  # one span per attempt, failures included
            try:
                # Enforce the timeout ourselves too: a hung provider must not outlive the deadline.
                resp = await asyncio.wait_for(provider.complete(req, d.model, timeout), timeout)
            except TimeoutError as e:
                raise GatewayError(ErrorKind.TIMEOUT, "attempt timed out", provider=d.provider) from e
            rec.success(resp)
            return resp

    async def complete(self, chain: list[Deployment], req: ChatRequest,
                       deadline: Deadline) -> tuple[ChatResponse, list[Attempt]]:
        self.budget.on_request()
        attempts: list[Attempt] = []
        last: GatewayError | None = None
        for d in chain:
            br = self.breaker(d)
            if not br.allow():
                attempts.append(Attempt(d.provider, d.model, 0, ErrorKind.CIRCUIT_OPEN))
                last = GatewayError(ErrorKind.CIRCUIT_OPEN, "all deployments unavailable")
                continue
            for n in range(1, self.policy.max_attempts + 1):
                try:
                    resp = await self._call(d, req, deadline.attempt_timeout(self.attempt_timeout), n)
                except GatewayError as e:
                    if e.kind == ErrorKind.DEADLINE:
                        e.attempts = attempts  # type: ignore[attr-defined]
                        raise
                    br.record(False)
                    last = e
                    if not e.retryable or n == self.policy.max_attempts or not br.allow():
                        attempts.append(Attempt(d.provider, d.model, n, e.kind))
                        break
                    if not self.budget.try_spend():
                        attempts.append(Attempt(d.provider, d.model, n, f"{e.kind}+no_budget"))
                        break
                    delay = self.policy.backoff(n, e.retry_after)
                    if delay >= deadline.remaining():
                        attempts.append(Attempt(d.provider, d.model, n, f"{e.kind}+no_time"))
                        break
                    attempts.append(Attempt(d.provider, d.model, n, e.kind, round(delay, 3)))
                    await self.clock.sleep(delay)
                    continue
                br.record(True)
                attempts.append(Attempt(d.provider, d.model, n, "ok"))
                return resp, attempts
            if last is not None and last.kind not in FALLBACKABLE:
                last.attempts = attempts  # type: ignore[attr-defined]
                raise last  # a 400 on the primary will be a 400 on the fallback too
        assert last is not None
        last.attempts = attempts  # type: ignore[attr-defined]
        raise last

    async def stream(self, chain: list[Deployment], req: ChatRequest,
                     deadline: Deadline) -> AsyncIterator[StreamChunk]:
        """Fail over freely until the first chunk reaches the caller. After that, never.

        Once bytes are on the wire you can't splice a different model's answer onto half of
        this one. A mid-stream failure becomes an explicit error chunk the client must handle.
        """
        self.budget.on_request()
        last: GatewayError | None = None
        for d in chain:
            br = self.breaker(d)
            if not br.allow():
                last = GatewayError(ErrorKind.CIRCUIT_OPEN, "all deployments unavailable")
                continue
            started = False
            try:
                with self.instrument(d, req, 1) as rec:
                    gen = self.providers[d.provider].stream(
                        req, d.model, deadline.attempt_timeout(self.attempt_timeout))
                    async for chunk in gen:
                        if not started and chunk.text:
                            rec.first_chunk()
                        started = started or bool(chunk.text)
                        if chunk.usage is not None:
                            rec.success(None, chunk.usage, chunk.finish_reason)
                            chunk = replace(chunk, model=d.model)
                        yield chunk
                br.record(True)
                return
            except GatewayError as e:
                br.record(False)
                if started:
                    yield StreamChunk(finish_reason="error")
                    return
                last = e
                if e.kind not in FALLBACKABLE:
                    raise
        assert last is not None
        raise last


async def hedged(primary: Callable[[], Awaitable[ChatResponse]],
                 backup: Callable[[], Awaitable[ChatResponse]], after: float) -> tuple[ChatResponse, str]:
    """Start `backup` if `primary` hasn't answered within `after` seconds; first answer wins.

    Cuts tail latency at the cost of paying twice for the slowest few percent. Only for
    idempotent calls, and set `after` near your p95, not your median.
    """
    t1 = asyncio.create_task(primary())
    done, _ = await asyncio.wait({t1}, timeout=after)
    if done:
        return t1.result(), "primary"
    t2 = asyncio.create_task(backup())
    done, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()  # cancelling stops *our* wait; the provider may still bill the tokens
    winner = done.pop()
    return winner.result(), "primary" if winner is t1 else "backup"
