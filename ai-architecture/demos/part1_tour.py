"""Part 1: one request through the whole platform, then the same request over HTTP."""

import asyncio

import httpx2

from aigw.app import create_app
from aigw.build import build_lab
from aigw.telemetry import span_tree
from aigw.types import ChatRequest, Message

SYSTEM = "You are the support assistant for Acme. Answer briefly."


async def main() -> None:
    lab = build_lab()
    req = ChatRequest(
        model="chat-default",
        messages=[Message("system", SYSTEM),
                  Message("user", "My card 4111 1111 1111 1111 was charged twice, email me at "
                                  "jane.doe@example.com")],
        max_tokens=300, metadata={"feature": "support-bot", "prompt_version": "v3"})
    r = await lab.gateway.chat(lab.acme_key, req)

    print("== what the upstream saw (last user message) ==")
    print(repr(lab.anthropic.seen[-1]))
    print("== what the caller got ==")
    print(f"served_by={r.response.provider}/{r.response.model} finish={r.response.finish_reason}")
    print(f"text={r.response.text!r}")
    print(f"usage={r.response.usage}")
    print(f"cost_usd={r.cost_usd:.6f}")
    print(f"guardrails={r.guard_reasons}")
    print(f"attempts={[(a.provider, a.model, a.n, a.outcome) for a in r.attempts]}")
    print("== trace ==")
    print(span_tree(lab.gateway.tel.finished(), keys=(
        "aigw.tenant", "aigw.feature", "aigw.guardrail.verdict", "gen_ai.provider.name",
        "gen_ai.request.model", "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens",
        "aigw.cost_usd")))

    print("\n== the same gateway over HTTP ==")
    app = create_app(lab.gateway)
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app),
                                  base_url="http://aigw.internal") as http:
        ok = await http.post("/v1/chat", headers={"authorization": f"Bearer {lab.acme_key}"},
                             json={"model": "chat-default", "max_tokens": 200,
                                   "messages": [{"role": "user", "content": "What is a gateway?"}]})
        print(ok.status_code, ok.json())
        bad = await http.post("/v1/chat", headers={"authorization": "Bearer aigw_live_nope"},
                              json={"model": "chat-default",
                                    "messages": [{"role": "user", "content": "hi"}]})
        print(bad.status_code, bad.json())
        unknown = await http.post("/v1/chat", headers={"authorization": f"Bearer {lab.acme_key}"},
                                  json={"model": "claude-sonnet-5-5",
                                        "messages": [{"role": "user", "content": "hi"}]})
        print(unknown.status_code, unknown.json())


if __name__ == "__main__":
    asyncio.run(main())
