"""Behaviour the series relies on. Fast, offline, deterministic: these run on every commit."""

import asyncio

import pytest

from aigw.build import build_lab
from aigw.fakes import FakeUpstream, anthropic_client
from aigw.guardrails import (GuardContext, GuardrailPipeline, PiiRedactor, SecretScanner,
                             Verdict, guarded_stream)
from aigw.limits import BudgetLedger, TokenRateLimiter
from aigw.providers.anthropic_provider import AnthropicProvider
from aigw.resilience import BreakerState, CircuitBreaker, FakeClock
from aigw.security import (AgentContext, Effect, ToolCall, ToolDecision, ToolPolicy, ToolSpec,
                           idempotency_key)
from aigw.types import ChatRequest, ErrorKind, GatewayError, Message, Principal, StreamChunk, Usage


def run(coro):
    return asyncio.run(coro)


def ask(text="hello", **kw):
    return ChatRequest("chat-default", [Message("user", text)], max_tokens=100, **kw)


# --- contract ---------------------------------------------------------------------------

def test_anthropic_usage_is_normalised_to_include_cache_tokens():
    fake = FakeUpstream("anthropic")
    p = AnthropicProvider(anthropic_client(fake))
    req = ChatRequest("x", [Message("system", "policy " * 1000), Message("user", "hi")])
    run(p.complete(req, "claude-sonnet-5-5", 5))
    u = run(p.complete(req, "claude-sonnet-5-5", 5)).usage
    assert u.cache_read_tokens > 0
    assert u.input_tokens == u.uncached_input_tokens + u.cache_read_tokens


def test_midstream_error_on_a_200_is_classified_by_body_type():
    lab = build_lab(anthropic_script=["midstream"])
    out = run(_collect(lab.gateway.stream(lab.acme_key, ask("tell me a story"))))
    assert out.endswith("[stream interrupted]")
    assert lab.selfhosted.calls == 0  # no failover once bytes were sent


async def _collect(gen):
    return "".join([t async for t in gen])


def test_plain_500_is_not_reported_as_mid_stream():
    fake = FakeUpstream("anthropic", [500])
    with pytest.raises(GatewayError) as e:
        run(AnthropicProvider(anthropic_client(fake)).complete(ask(), "m", 5))
    assert e.value.kind == ErrorKind.UPSTREAM and e.value.message == "upstream 500"


# --- resilience -------------------------------------------------------------------------

def test_sdk_retries_are_disabled_inside_the_gateway():
    lab = build_lab(anthropic_script=[529] * 10)
    run(lab.gateway.chat(lab.acme_key, ask()))
    assert lab.anthropic.calls == 3  # gateway attempts only, not 3 x 3


def test_client_errors_are_not_retried_or_failed_over():
    lab = build_lab(anthropic_script=[400])
    with pytest.raises(GatewayError) as e:
        run(lab.gateway.chat(lab.acme_key, ask()))
    assert e.value.kind == ErrorKind.INVALID_REQUEST
    assert lab.anthropic.calls == 1 and lab.selfhosted.calls == 0


def test_failover_before_first_byte():
    lab = build_lab(anthropic_script=[529])
    out = run(_collect(lab.gateway.stream(lab.acme_key, ask("hi there"))))
    assert "hi there" in out and lab.selfhosted.calls == 1


def test_breaker_opens_half_opens_and_closes():
    clock = FakeClock()
    br = CircuitBreaker(clock, window=10, min_calls=4, failure_rate=0.5, cooldown=30)
    for _ in range(4):
        br.record(False)
    assert br.state == BreakerState.OPEN and not br.allow()
    clock.advance(30)
    assert br.allow() and br.state == BreakerState.HALF_OPEN
    assert not br.allow()  # one probe at a time
    br.record(True)
    assert br.state == BreakerState.CLOSED


# --- cost and quota ---------------------------------------------------------------------

def test_token_limiter_reconciles_reservations():
    lim = TokenRateLimiter(FakeClock(), rpm=100, tpm=10_000)
    r = lim.reserve("k", ChatRequest("a", [Message("user", "hi")], max_tokens=4000))
    lim.reconcile(r, Usage(10, 20))
    assert lim.tok_buckets["k"].level == pytest.approx(10_000 - 30)


def test_budget_holds_block_concurrent_overspend():
    ledger = BudgetLedger()
    ledger.set_limit("tenant:t", 1.0)
    ledger.hold(("tenant:t",), 0.6)
    with pytest.raises(GatewayError) as e:
        ledger.hold(("tenant:t",), 0.6)  # would pass a naive spent-only check
    assert e.value.kind == ErrorKind.BUDGET_EXCEEDED


def test_cache_is_tenant_scoped():
    lab = build_lab()
    lab.gateway.budgets.set_limit("tenant:globex", 1.0)
    req = lambda: ask("same question", metadata={"cache": "exact"})  # noqa: E731
    run(lab.gateway.chat(lab.acme_key, req()))
    r = run(lab.gateway.chat(lab.globex_key, req()))
    assert not r.response.cached and lab.anthropic.calls == 2


def test_cache_never_serves_one_users_pii_to_another():
    """Regression: the key is computed on redacted text, so both prompts share it."""
    lab = build_lab()
    carol, _ = lab.keys.issue("acme", "carol", {"chat"})
    q = lambda e: ask(f"Write to {e} about the refund", metadata={"cache": "exact"})  # noqa: E731
    run(lab.gateway.chat(lab.acme_key, q("alice@acme.test")))
    r = run(lab.gateway.chat(carol, q("carol@acme.test")))
    assert r.response.cached
    assert "alice@acme.test" not in r.response.text and "carol@acme.test" in r.response.text


# --- guardrails ---------------------------------------------------------------------------

def test_only_the_users_own_pii_is_restored():
    lab = build_lab(anthropic_responder=lambda p: "Contact stranger@globex.test instead")
    r = run(lab.gateway.chat(lab.acme_key, ask("I am me@acme.test")))
    assert "stranger@globex.test" not in r.response.text


def test_card_redaction_respects_luhn_and_word_boundaries():
    res = run(PiiRedactor().check(GuardContext("input", "card 4242 4242 4242 4242 now, order 1234 5678 9012 3456")))
    assert res.text == "card <CARD_1> now, order 1234 5678 9012 3456"


def test_verdicts_only_get_stricter():
    r = run(GuardrailPipeline([PiiRedactor(), SecretScanner()]).run(
        GuardContext("output", "mail a@b.test the key AKIAIOSFODNN7EXAMPLE")))
    assert r.verdict == Verdict.BLOCK


def test_streaming_holdback_stops_split_secret():
    token = "ghp_" + "x" * 36

    async def chunks():
        for i in range(0, len(token), 8):
            yield StreamChunk(text=token[i:i + 8])

    out = run(_collect(guarded_stream(chunks(), GuardrailPipeline([SecretScanner()]), None, holdback=64)))
    assert "ghp_" not in out


# --- security -----------------------------------------------------------------------------

def test_taint_requires_approval_for_side_effects():
    p = Principal("acme", "k", scopes=frozenset({"w", "r"}))
    pol = ToolPolicy([ToolSpec("write", Effect.WRITE, "w"), ToolSpec("read", Effect.READ, "r")])
    ctx = AgentContext(p)
    assert pol.authorize(ToolCall("write", {}), ctx)[0] == ToolDecision.ALLOW
    ctx.ingest("web:page", trusted=False)
    assert pol.authorize(ToolCall("write", {}), ctx)[0] == ToolDecision.APPROVE
    assert pol.authorize(ToolCall("read", {}), ctx)[0] == ToolDecision.ALLOW


def test_idempotency_key_is_stable_across_retries_and_arg_order():
    a = idempotency_key("conv-1", 3, ToolCall("issue_refund", {"order": "a-1", "eur": 40}))
    b = idempotency_key("conv-1", 3, ToolCall("issue_refund", {"eur": 40, "order": "a-1"}))
    c = idempotency_key("conv-1", 4, ToolCall("issue_refund", {"order": "a-1", "eur": 40}))
    assert a == b and a != c
