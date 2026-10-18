# aigw: the lab for "Building a Robust AI Platform"

Companion code for the 8-part series on [zdenovo.com](https://zdenovo.com/series/ai-platform-architecture-deep-dive):
an LLM gateway in Python with provider adapters, resilience, cost controls, security policy,
guardrails and OpenTelemetry instrumentation. It's a teaching implementation, not a product:
every design decision in it is one you'd make in a real gateway's config.

```bash
cd ai-architecture
./run_all.sh            # every demo + the tests, offline, ~10 s
uv run python -m demos.part3_resilience
uv run pytest -q
```

No API keys needed. Upstream providers are **scripted fakes** (`aigw/fakes.py`) behind
`httpx2.MockTransport` that speak the real Anthropic Messages (JSON + SSE) and
chat-completions (JSON + SSE) wire formats, so the official `anthropic` SDK and the adapters run
for real; only the model's *text* is canned. `outputs/` holds the captured runs the articles
quote. Expected run-to-run differences: random key IDs, millisecond timings, and the tail
sampler's baseline count.

| Module | What | Part |
|---|---|---|
| `aigw/types.py` | canonical contract, usage semantics, error taxonomy | 2 |
| `aigw/catalog.py` | aliases → deployment chains, capabilities, tenants, prices | 2, 4 |
| `aigw/providers/` | Claude (official SDK) and self-hosted (chat-completions) adapters | 2 |
| `aigw/resilience.py` | deadline, full-jitter retry, retry budget, circuit breaker, fallback, stream failover, hedging | 3 |
| `aigw/limits.py`, `aigw/cache.py` | token-aware rate limits, budget holds, exact + (toy) semantic cache | 4 |
| `aigw/security.py` | virtual keys, tool policy with taint, idempotency keys, ACL retrieval, link neutralising | 5 |
| `aigw/guardrails.py` | tiered rail pipeline, PII vault, secret scanner, streaming holdback, bounded repair | 6 |
| `aigw/telemetry.py` | OTel GenAI spans + metrics, tail sampler | 7 |
| `aigw/checks.py` | CI checks over catalog, limits and prompts | 8 |
| `aigw/gateway.py`, `aigw/app.py` | the request pipeline and a FastAPI front | all |

Captured with CPython 3.14.4, `anthropic` 1.15.0 (on `httpx2`), `opentelemetry-sdk` 1.45.1.
Prices in `catalog.py` are as listed on 2026-10-06; check current ones before reusing them.
Credential-looking strings in demos and tests are fakes (and AWS's documented example key),
there to exercise the secret scanner.
