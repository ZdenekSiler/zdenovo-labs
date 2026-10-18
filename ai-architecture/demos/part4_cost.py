"""Part 4: cost attribution, token-aware limits, budgets, and caches."""

import asyncio
from datetime import datetime, timedelta

from aigw.build import build_lab
from aigw.cache import SemanticCache, cosine, toy_embed
from aigw.catalog import HAIKU, OPUS, SONNET
from aigw.limits import TokenRateLimiter
from aigw.resilience import FakeClock
from aigw.types import ChatRequest, ChatResponse, GatewayError, Message, Usage

POLICY = "You are Acme's support assistant. Follow the refund policy below.\n" + "Policy clause. " * 600


def req(system: str, text: str, **kw) -> ChatRequest:
    return ChatRequest("chat-default", [Message("system", system), Message("user", text)],
                       max_tokens=kw.pop("max_tokens", 300), **kw)


async def prompt_caching() -> None:
    print("== 1. prompt caching: same prefix vs a timestamp in the system prompt ==")
    lab = build_lab()
    stable = [await lab.gateway.chat(lab.acme_key, req(POLICY, f"question {i}")) for i in range(5)]
    print("stable prefix  :", [f"${r.cost_usd:.5f}" for r in stable],
          f"total ${sum(r.cost_usd for r in stable):.5f}")
    lab = build_lab()
    t0 = datetime(2026, 10, 18, 9, 0, 0)
    drift = [await lab.gateway.chat(lab.acme_key, req(f"Current time: {t0 + timedelta(seconds=i)}\n" + POLICY,
                                                      f"question {i}")) for i in range(5)]
    print("timestamped    :", [f"${r.cost_usd:.5f}" for r in drift],
          f"total ${sum(r.cost_usd for r in drift):.5f}")
    print("usage call 2   :", stable[1].response.usage, "vs", drift[1].response.usage)


async def token_limits() -> None:
    print("\n== 2. token-aware rate limiting: reserve worst case, reconcile actual ==")
    clock = FakeClock()
    lim = TokenRateLimiter(clock, rpm=100, tpm=10_000)
    small = ChatRequest("chat-default", [Message("user", "Short question?")], max_tokens=4096)
    held = []
    for i in range(4):
        try:
            held.append(lim.reserve("key-a", small))
            print(f"reserve #{i}: ok, reserved {held[-1].tokens}, bucket={lim.tok_buckets['key-a'].level:.0f}")
        except GatewayError as e:
            print(f"reserve #{i}: {e.kind.value} ({e.message}), retry_after={e.retry_after}s")
    for r in held:
        lim.reconcile(r, Usage(input_tokens=12, output_tokens=40))
    print(f"after reconciling actual usage (52 tokens each): bucket={lim.tok_buckets['key-a'].level:.0f}")
    huge = ChatRequest("chat-default", [Message("user", "x" * 60_000)], max_tokens=1000)
    try:
        lim.reserve("key-a", huge)
    except GatewayError as e:
        print(f"oversized request: {e.kind.value}, retry_after={e.retry_after} (never fits: don't ask them to wait)")


async def budgets() -> None:
    print("\n== 3. hierarchical budgets with holds: globex has $0.002/month ==")
    lab = build_lab()
    for i in range(60):
        try:
            r = await lab.gateway.chat(lab.globex_key, req("Be brief.", f"question {i}", max_tokens=50))
            if i % 5 == 0 or i > 22:
                print(f"req {i}: ok ${r.cost_usd:.6f}  spent=${lab.gateway.budgets.spent_usd('tenant:globex'):.6f}")
        except GatewayError as e:
            print(f"req {i}: {e.kind.value} ({e.message}) HTTP {e.status}")
            break
    print("alerts:", lab.gateway.budgets.alerts)
    try:
        await lab.gateway.chat(lab.acme_key, req("Be brief.", "hi", max_tokens=8192))
        print("acme 8192-token request: ok (acme has $50)")
    except GatewayError as e:
        print("acme:", e.kind.value)


async def exact_cache() -> None:
    print("\n== 4. exact cache: opt-in, tenant-scoped, PII-safe ==")
    lab = build_lab()
    lab.gateway.budgets.set_limit("tenant:globex", 1.0)
    q = lambda who, email: ChatRequest(  # noqa: E731
        "chat-default", [Message("user", f"Draft a refund confirmation to {email}")],
        max_tokens=200, metadata={"cache": "exact"})
    a = await lab.gateway.chat(lab.acme_key, q("alice", "alice@acme.test"))
    alice2_key, _ = lab.keys.issue("acme", "carol", {"chat"})
    b = await lab.gateway.chat(alice2_key, q("carol", "carol@acme.test"))
    c = await lab.gateway.chat(lab.globex_key, q("bob", "bob@globex.test"))
    for name, r in (("alice/acme", a), ("carol/acme", b), ("bob/globex", c)):
        print(f"{name:11} cached={r.response.cached!s:5} cost=${r.cost_usd:.6f} text={r.response.text!r}")
    print(f"upstream calls={lab.anthropic.calls}, hits={lab.gateway.cache.hits}, misses={lab.gateway.cache.misses}")


async def semantic_cache() -> None:
    print("\n== 5. semantic cache: why similarity is not equivalence (toy embedding) ==")
    sc = SemanticCache(threshold=0.85)
    cached = "How do I cancel my order?"
    sc.put("acme", cached, ChatResponse("1", "x", "m", "Go to Orders > Cancel.", "stop", Usage()))
    for probe in ("how do I cancel my order", "How do I NOT cancel my order?",
                  "How do I cancel my order? I don't want to cancel my subscription",
                  "Where do I see my order?"):
        sim = cosine(toy_embed(cached), toy_embed(probe))
        hit = sc.get("acme", probe)
        print(f"{sim:.2f} {'HIT ' if hit else 'miss'} {probe!r}" + (f" -> {hit.response.text!r}" if hit else ""))


async def model_mix() -> None:
    print("\n== 6. the same month of traffic on three models ==")
    # 1M requests: 2,000-token system prompt (cached after the first), 300 user tokens, 250 out.
    n, sys_t, user_t, out_t = 1_000_000, 2_000, 300, 250
    for name, price in (("haiku-5-5", HAIKU), ("sonnet-5-5", SONNET), ("opus-5-5", OPUS)):
        no_cache = price.cost(Usage(input_tokens=sys_t + user_t, output_tokens=out_t)) * n
        cached = price.cost(Usage(input_tokens=sys_t + user_t, output_tokens=out_t,
                                  cache_read_tokens=sys_t)) * n
        note = "  (cache-read price unknown: billed at full input)" if price.cache_read is None else ""
        print(f"{name:11} no cache ${no_cache:>9,.0f}   cached prefix ${cached:>9,.0f}{note}")


async def main() -> None:
    for step in (prompt_caching, token_limits, budgets, exact_cache, semantic_cache, model_mix):
        await step()


if __name__ == "__main__":
    asyncio.run(main())
