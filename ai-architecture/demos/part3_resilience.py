"""Part 3: retries, budgets, breakers, fallbacks, deadlines, streaming failover, hedging."""

import asyncio
import time

from aigw.build import build_lab
from aigw.catalog import default_catalog
from aigw.fakes import FakeUpstream, anthropic_client, selfhosted_client
from aigw.providers.anthropic_provider import AnthropicProvider
from aigw.providers.selfhosted import SelfHostedProvider
from aigw.resilience import (Clock, Deadline, FakeClock, ResilientRouter, RetryBudget,
                             RetryPolicy, hedged)
from aigw.types import ChatRequest, GatewayError, Message


def ask(text="Where is my parcel?", **kw) -> ChatRequest:
    return ChatRequest("chat-default", [Message("user", text)], max_tokens=200, **kw)


def show(attempts) -> str:
    return " | ".join(f"{a.provider}#{a.n}:{a.outcome}" + (f"(slept {a.slept}s)" if a.slept else "")
                      for a in attempts)


async def amplification() -> None:
    print("== 1. retry amplification: SDK retries inside gateway retries ==")
    for sdk_retries in (2, 0):
        lab = build_lab(anthropic_script=[529] * 50, sdk_retries=sdk_retries)
        r = await lab.gateway.chat(lab.acme_key, ask())
        print(f"sdk max_retries={sdk_retries}: upstream calls to anthropic={lab.anthropic.calls}, "
              f"gateway saw {sum(a.provider == 'anthropic' for a in r.attempts)} attempts, "
              f"served by {r.response.model}")


async def backoff_schedule() -> None:
    print("\n== 2. full-jitter backoff, and Retry-After wins when it's longer ==")
    p = RetryPolicy()
    print("ceilings :", [min(p.cap, p.base * 2 ** n) for n in range(1, 6)])
    print("sampled  :", [round(p.backoff(n, None), 3) for n in range(1, 6)])
    print("429 w/ Retry-After=1 ->", round(p.backoff(1, 1.0), 3))


async def outage() -> None:
    print("\n== 3. primary outage: breaker + retry budget + fallback, 40 requests ==")
    lab = build_lab(anthropic_script=[529] * 1000)
    rows = []
    for i in range(40):
        r = await lab.gateway.chat(lab.acme_key, ask(f"question {i}"))
        rows.append((i, show(r.attempts)))
        lab.clock.advance(0.5)
    for i, s in rows[:4] + [("...", "...")] + rows[-2:]:
        print(f"req {i}: {s}")
    br = lab.gateway.router.breakers["anthropic/claude-sonnet-5-5"]
    print("breaker transitions:", br.transitions)
    print(f"anthropic upstream calls for 40 requests: {lab.anthropic.calls}")
    print(f"retry budget tokens left: {lab.gateway.router.budget.tokens:.1f}")

    print("-- primary recovers; time passes past the cooldown --")
    lab.anthropic.script.clear()
    lab.clock.advance(31)
    for i in range(3):
        r = await lab.gateway.chat(lab.acme_key, ask(f"after {i}"))
        print(f"req after+{i}: {show(r.attempts)}")
    print("breaker transitions:", br.transitions[-2:])


async def no_budget_comparison() -> None:
    print("\n== 4. same outage without breaker or budget (what most code does) ==")
    from aigw.resilience import CircuitBreaker
    lab = build_lab(anthropic_script=[529] * 1000, retry_budget=RetryBudget(ratio=1, reserve=1e9))
    lab.gateway.router._breaker_factory = lambda: CircuitBreaker(lab.clock, min_calls=10**9)
    for i in range(40):
        await lab.gateway.chat(lab.acme_key, ask(f"question {i}"))
        lab.clock.advance(0.5)
    print(f"anthropic upstream calls for 40 requests: {lab.anthropic.calls}, "
          f"virtual time spent sleeping in backoff: {lab.clock.t - 20:.1f}s")


async def non_retryable() -> None:
    print("\n== 5. a 400 is not retried and not failed over ==")
    lab = build_lab(anthropic_script=[400])
    try:
        await lab.gateway.chat(lab.acme_key, ask())
    except GatewayError as e:
        print(e.kind.value, "| attempts:", show(e.attempts), "| selfhosted calls:", lab.selfhosted.calls)


async def deadline() -> None:
    print("\n== 6. one deadline for the whole request (real clock) ==")
    fa, fs = FakeUpstream("anthropic", ["slow:2"] * 10), FakeUpstream("selfhosted", ["slow:2"] * 10)
    router = ResilientRouter({"anthropic": AnthropicProvider(anthropic_client(fa)),
                              "selfhosted": SelfHostedProvider("http://gpu", selfhosted_client(fs))},
                             clock=Clock(), policy=RetryPolicy(base=0.05), attempt_timeout=0.3)
    chain = default_catalog().resolve("chat-default", "acme").chain
    t0 = time.perf_counter()
    try:
        await router.complete(chain, ask(), Deadline.after(router.clock, 1.0))
    except GatewayError as e:
        print(f"{e.kind.value} after {time.perf_counter() - t0:.1f}s | {show(e.attempts)}")


async def streaming() -> None:
    print("\n== 7. streaming: failover before the first byte, never after ==")
    for script in ([529], ["midstream"]):
        lab = build_lab(anthropic_script=script)
        out = [t async for t in lab.gateway.stream(lab.acme_key, ask("Tell me about breakers"))]
        print(f"primary={script[0]!s:9} -> {''.join(out)!r}  (selfhosted calls={lab.selfhosted.calls})")


async def hedging() -> None:
    print("\n== 8. hedging a slow primary (real clock) ==")
    fa, fs = FakeUpstream("anthropic", ["slow:0.5"]), FakeUpstream("selfhosted")
    a = AnthropicProvider(anthropic_client(fa))
    b = SelfHostedProvider("http://gpu", selfhosted_client(fs))
    t0 = time.perf_counter()
    resp, winner = await hedged(lambda: a.complete(ask(), "claude-sonnet-5-5", 5),
                                lambda: b.complete(ask(), "llama-local-70b", 5), after=0.1)
    print(f"winner={winner} ({resp.model}) in {time.perf_counter() - t0:.1f}s; "
          f"upstream calls: anthropic={fa.calls} selfhosted={fs.calls}")


async def main() -> None:
    for step in (amplification, backoff_schedule, outage, no_budget_comparison, non_retryable,
                 deadline, streaming, hedging):
        await step()


if __name__ == "__main__":
    asyncio.run(main())
