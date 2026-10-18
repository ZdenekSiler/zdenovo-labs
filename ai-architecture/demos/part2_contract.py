"""Part 2: one contract, many providers."""

import asyncio

from aigw.catalog import default_catalog
from aigw.fakes import FakeUpstream, anthropic_client, selfhosted_client
from aigw.providers.anthropic_provider import AnthropicProvider
from aigw.providers.selfhosted import SelfHostedProvider
from aigw.types import ChatRequest, GatewayError, Message

LONG_SYSTEM = "You are Acme's support assistant. " + "Policy text. " * 400  # ~1.3k tokens


async def main() -> None:
    fa, fs = FakeUpstream("anthropic"), FakeUpstream("selfhosted")
    claude = AnthropicProvider(anthropic_client(fa))
    local = SelfHostedProvider("http://gpu-pool.internal:8000", selfhosted_client(fs))
    req = ChatRequest("chat-default", [Message("system", LONG_SYSTEM),
                                       Message("user", "Where is my parcel?")])

    print("== same canonical request, two wire formats ==")
    for p, model in ((claude, "claude-sonnet-5-5"), (local, "llama-local-70b")):
        r = await p.complete(req, model, timeout=10)
        print(f"{r.provider:10} {r.model:18} finish={r.finish_reason} {r.usage}")

    print("\n== usage normalisation across two identical calls (prompt caching) ==")
    raw = await claude.client.messages.create(
        model="claude-sonnet-5-5", max_tokens=100,
        system=[{"type": "text", "text": LONG_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": "Where is my parcel?"}])
    print("raw Anthropic usage :", f"input_tokens={raw.usage.input_tokens}",
          f"cache_read={raw.usage.cache_read_input_tokens}",
          f"cache_write={raw.usage.cache_creation_input_tokens}")
    r = await claude.complete(req, "claude-sonnet-5-5", timeout=10)
    print("normalised (OTel)   :", r.usage, f"uncached={r.usage.uncached_input_tokens}")

    print("\n== error taxonomy: what each upstream failure becomes ==")
    print(f"{'provider':10} {'upstream':9} {'kind':18} {'retryable':9} {'status':6} body")
    for prov, fake, model, steps in ((claude, fa, "claude-sonnet-5-5", [429, 529, 500, 400, "timeout", "connect"]),
                                     (local, fs, "llama-local-70b", [429, 503, 500, 400, "timeout", "connect"])):
        for step in steps:
            fake.script = [step]
            try:
                await prov.complete(req, model, timeout=10)
            except GatewayError as e:
                print(f"{prov.name:10} {str(step):9} {e.kind.value:18} {str(e.retryable):9} {e.status:<6} {e.to_body()}")

    print("\n== routing by capability and tenant ==")
    cat = default_catalog()
    for alias, tenant, needs in (("chat-default", "acme", set()), ("chat-default", "acme", {"tools"}),
                                 ("chat-eu-only", "acme", {"tools"}), ("chat-premium", "acme", set()),
                                 ("chat-premium", "globex", set())):
        try:
            route = cat.resolve(alias, tenant, needs)
            print(f"{alias:13} {tenant:7} needs={sorted(needs)!s:10} -> {[d.model for d in route.chain]}")
        except GatewayError as e:
            print(f"{alias:13} {tenant:7} needs={sorted(needs)!s:10} -> {e.kind.value}: {e.message}")

    print("\n== streaming: two SSE dialects, one chunk type ==")
    short = ChatRequest("chat-default", [Message("user", "Explain idempotency keys")], stream=True)
    for p, model in ((claude, "claude-sonnet-5-5"), (local, "llama-local-70b")):
        chunks = [c async for c in p.stream(short, model, timeout=10)]
        texts = [c.text for c in chunks if c.text]
        print(f"{p.name:10} {len(texts)} text chunks, final={chunks[-1].finish_reason} {chunks[-1].usage}")


if __name__ == "__main__":
    asyncio.run(main())
