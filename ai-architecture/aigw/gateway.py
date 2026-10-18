"""The gateway: one object that runs every request through the same ordered pipeline.

    authenticate -> resolve route -> input guardrails -> cache -> quota + budget hold
      -> resilient routing (retries, breakers, fallbacks) -> output guardrails
      -> settle quota + budget -> telemetry
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace

from .cache import ExactCache, cache_key
from .catalog import ModelCatalog
from .guardrails import GuardContext, GuardrailPipeline, PiiVault, Verdict, guarded_stream
from .limits import BudgetLedger, TokenRateLimiter, worst_case_cost
from .resilience import Attempt, Clock, Deadline, ResilientRouter
from .security import KeyStore, neutralize_links
from .telemetry import Telemetry
from .types import ChatRequest, ChatResponse, ErrorKind, GatewayError, Message, Principal


@dataclass
class GatewayResult:
    response: ChatResponse
    cost_usd: float
    attempts: list[Attempt] = field(default_factory=list)
    guard_reasons: list[str] = field(default_factory=list)
    removed_links: list[str] = field(default_factory=list)


@dataclass
class GatewayConfig:
    request_deadline: float = 60.0
    allowed_link_hosts: set[str] = field(default_factory=lambda: {"docs.example.com"})
    restore_pii_in_output: bool = True  # the user's own PII back to the same user


class Gateway:
    def __init__(self, *, keys: KeyStore, catalog: ModelCatalog, router: ResilientRouter,
                 limiter: TokenRateLimiter, budgets: BudgetLedger, input_rails: GuardrailPipeline,
                 output_rails: GuardrailPipeline, telemetry: Telemetry,
                 cache: ExactCache | None = None, clock: Clock | None = None,
                 config: GatewayConfig | None = None):
        self.keys, self.catalog, self.router = keys, catalog, router
        self.limiter, self.budgets = limiter, budgets
        self.input_rails, self.output_rails = input_rails, output_rails
        self.tel, self.cache = telemetry, cache
        self.clock = clock or router.clock
        self.config = config or GatewayConfig()
        router.instrument = telemetry.inference  # every attempt becomes a span

    @staticmethod
    def scopes(p: Principal, req: ChatRequest) -> tuple[str, ...]:
        s = [f"tenant:{p.tenant}", f"key:{p.key_id}"]
        if feature := req.metadata.get("feature"):
            s.append(f"feature:{p.tenant}/{feature}")
        return tuple(s)

    async def _guard_input(self, p: Principal, req: ChatRequest, vault: PiiVault) -> tuple[ChatRequest, list[str]]:
        reasons: list[str] = []
        new_messages: list[Message] = []
        with self.tel.guard("input") as span:
            worst = Verdict.ALLOW
            for m in req.messages:
                if m.role == "system":  # operator-authored, trusted, and stable for caching
                    new_messages.append(m)
                    continue
                res = await self.input_rails.run(GuardContext("input", m.content, p, vault))
                self.tel.record_guard(span, "input", res)
                worst = max(worst, res.verdict)
                reasons += res.reasons
                new_messages.append(Message(m.role, res.text))
            span.set_attribute("aigw.guardrail.verdict", worst.name)
        if worst >= Verdict.REVIEW:
            raise GatewayError(ErrorKind.BLOCKED, "request blocked by input policy")
        return replace(req, messages=new_messages), reasons

    async def _guard_output(self, p: Principal, text: str, vault: PiiVault) -> tuple[str, list[str]]:
        with self.tel.guard("output") as span:
            res = await self.output_rails.run(GuardContext("output", text, p, vault))
            self.tel.record_guard(span, "output", res)
        if res.verdict >= Verdict.REVIEW:
            raise GatewayError(ErrorKind.BLOCKED, "response withheld by output policy")
        return res.text, res.reasons

    async def chat(self, api_key: str | None, req: ChatRequest) -> GatewayResult:
        with self.tel.request(req.model, stream=False) as span:
            try:
                result = await self._chat(api_key, req, span)
            except GatewayError as e:
                span.set_attribute("error.type", e.kind.value)
                self.tel.requests.add(1, {"aigw.alias": req.model, "error.type": e.kind.value})
                raise
            self.tel.requests.add(1, {"aigw.alias": req.model})
            return result

    async def _chat(self, api_key: str | None, req: ChatRequest, span) -> GatewayResult:
        p = self.keys.authenticate(api_key)
        self.tel.annotate_principal(span, p, req)
        route = self.catalog.resolve(req.model, p.tenant)
        req.max_tokens = min(req.max_tokens, route.max_tokens_cap)
        vault = PiiVault()
        req, reasons = await self._guard_input(p, req, vault)
        # Only what the USER sent may be restored. PII the model emits (say, from a retrieved
        # document) is redacted by the output rails into the same vault and must stay redacted.
        users_own = set(vault.values)

        key = None
        if self.cache and req.metadata.get("cache") == "exact":
            key = cache_key(p.tenant, route.alias, req)  # computed on the REDACTED request
            hit = self.cache.get(key)
            self.tel.cache.add(1, {"aigw.cache.result": "hit" if hit else "miss"})
            if hit:
                span.set_attribute("aigw.cache.hit", True)
                # The cached text holds placeholders; restore with THIS request's vault.
                text = vault.restore(hit.text, users_own) if self.config.restore_pii_in_output else hit.text
                return GatewayResult(replace(hit, text=text, cached=True), 0.0, [], reasons)

        reservation = self.limiter.reserve(p.key_id, req)
        hold_usd = max(worst_case_cost(d.price, req) for d in route.chain)
        try:
            hold = self.budgets.hold(self.scopes(p, req), hold_usd)
        except GatewayError:
            self.limiter.reconcile(reservation, None)
            raise
        resp, cost = None, 0.0
        try:
            deadline = Deadline.after(self.clock, self.config.request_deadline)
            resp, attempts = await self.router.complete(route.chain, req, deadline)
            served = next(d for d in route.chain if d.model == resp.model)
            cost = served.price.cost(resp.usage)
        finally:
            self.limiter.reconcile(reservation, resp.usage if resp else None)
            self.budgets.settle(hold, cost)  # failed calls settle at 0 (or what you were billed)
        self.tel.record_cost(p, req, cost)
        span.set_attribute("aigw.cost_usd", round(cost, 8))
        span.set_attribute("aigw.served_by", f"{resp.provider}/{resp.model}")

        text, out_reasons = await self._guard_output(p, resp.text, vault)
        text, removed = neutralize_links(text, self.config.allowed_link_hosts)
        if key is not None and self.cache:
            # Cache BEFORE restoring PII. The key was computed on the redacted prompt, so two
            # users whose different emails both became <EMAIL_1> share a key. Cache the
            # restored text and user B reads user A's address (tests/test_cache_pii.py).
            self.cache.put(key, replace(resp, text=text))
        if self.config.restore_pii_in_output:
            text = vault.restore(text, users_own)
        return GatewayResult(replace(resp, text=text), cost, attempts, reasons + out_reasons, removed)

    async def stream(self, api_key: str | None, req: ChatRequest) -> AsyncIterator[str]:
        """Streaming path: same pipeline, but output rails run incrementally with holdback."""
        with self.tel.request(req.model, stream=True) as span:
            p = self.keys.authenticate(api_key)
            self.tel.annotate_principal(span, p, req)
            route = self.catalog.resolve(req.model, p.tenant, needs={"stream"})
            req.stream = True
            req.max_tokens = min(req.max_tokens, route.max_tokens_cap)
            vault = PiiVault()
            req, _ = await self._guard_input(p, req, vault)
            reservation = self.limiter.reserve(p.key_id, req)
            hold = self.budgets.hold(self.scopes(p, req),
                                     max(worst_case_cost(d.price, req) for d in route.chain))
            usage, cost = None, 0.0
            deadline = Deadline.after(self.clock, self.config.request_deadline)
            chunks = self.router.stream(route.chain, req, deadline)

            served = route.chain[0]

            async def tap():
                nonlocal usage, served
                async for c in chunks:
                    if c.usage is not None:
                        usage = c.usage
                        served = next((d for d in route.chain if d.model == c.model), served)
                    yield c

            try:
                async for text in guarded_stream(tap(), self.output_rails, p):
                    yield text
            finally:
                if usage is not None:
                    cost = served.price.cost(usage)  # priced at whoever actually served it
                self.limiter.reconcile(reservation, usage)
                self.budgets.settle(hold, cost)
                self.tel.record_cost(p, req, cost)
                span.set_attribute("aigw.cost_usd", round(cost, 8))
