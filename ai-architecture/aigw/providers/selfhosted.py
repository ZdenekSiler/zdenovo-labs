"""Adapter for a self-hosted model server (vLLM, llama.cpp, TGI...).

These servers expose the de-facto "chat completions" wire format, so one adapter covers most
of them. Raw httpx2 here because there's no vendor SDK for "our own GPU box".
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx2

from ..types import ChatRequest, ChatResponse, ErrorKind, GatewayError, StreamChunk, Usage

FINISH = {"stop": "stop", "length": "length", "tool_calls": "tool_call",
          "content_filter": "refusal"}


def _status_error(r: httpx2.Response) -> GatewayError:
    p = "selfhosted"
    if r.status_code == 429:
        ra = r.headers.get("retry-after")
        return GatewayError(ErrorKind.RATE_LIMITED, "upstream rate limit", provider=p,
                            retry_after=float(ra) if ra else None)
    if r.status_code == 503:
        return GatewayError(ErrorKind.OVERLOADED, "upstream overloaded", provider=p)
    if r.status_code == 400 and "context length" in r.text:
        return GatewayError(ErrorKind.CONTEXT_TOO_LONG, "context window exceeded", provider=p)
    if 400 <= r.status_code < 500:
        return GatewayError(ErrorKind.INVALID_REQUEST, "upstream rejected request", provider=p)
    return GatewayError(ErrorKind.UPSTREAM, f"upstream {r.status_code}", provider=p)


class SelfHostedProvider:
    name = "selfhosted"

    def __init__(self, base_url: str, client: httpx2.AsyncClient | None = None):
        self.base_url = base_url.rstrip("/")
        self.http = client or httpx2.AsyncClient()

    def _body(self, req: ChatRequest, model: str, stream: bool) -> dict:
        body = {"model": model, "max_tokens": req.max_tokens, "stream": stream,
                "messages": [{"role": m.role, "content": m.content} for m in req.messages]}
        if stream:
            body["stream_options"] = {"include_usage": True}
        if req.temperature is not None:
            body["temperature"] = req.temperature
        return body

    async def _post(self, body: dict, timeout: float) -> httpx2.Response:
        try:
            r = await self.http.post(f"{self.base_url}/v1/chat/completions", json=body,
                                     timeout=timeout)
        except httpx2.TimeoutException as e:
            raise GatewayError(ErrorKind.TIMEOUT, "upstream timed out", provider=self.name) from e
        except httpx2.TransportError as e:
            raise GatewayError(ErrorKind.CONNECTION, "upstream unreachable",
                               provider=self.name) from e
        if r.status_code >= 400:
            raise _status_error(r)
        return r

    async def complete(self, req: ChatRequest, model: str, timeout: float) -> ChatResponse:
        data = (await self._post(self._body(req, model, False), timeout)).json()
        choice = data["choices"][0]
        u = data.get("usage") or {}
        cached = (u.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
        return ChatResponse(
            id=data.get("id", ""), provider=self.name, model=data.get("model", model),
            text=choice["message"].get("content") or "",
            finish_reason=FINISH.get(choice.get("finish_reason"), "stop"),
            # chat-completions prompt_tokens already includes cached tokens: OTel semantics.
            usage=Usage(u.get("prompt_tokens", 0), u.get("completion_tokens", 0), cached))

    async def stream(self, req: ChatRequest, model: str, timeout: float) -> AsyncIterator[StreamChunk]:
        finish = None
        try:
            async with self.http.stream("POST", f"{self.base_url}/v1/chat/completions",
                                        json=self._body(req, model, True), timeout=timeout) as r:
                if r.status_code >= 400:
                    await r.aread()
                    raise _status_error(r)
                async for line in r.aiter_lines():
                    if not line.startswith("data: ") or line == "data: [DONE]":
                        continue
                    data = json.loads(line[6:])
                    for choice in data.get("choices", []):
                        if text := (choice.get("delta") or {}).get("content"):
                            yield StreamChunk(text=text)
                        finish = choice.get("finish_reason") or finish
                    if u := data.get("usage"):
                        yield StreamChunk(finish_reason=FINISH.get(finish, "stop"),
                                          usage=Usage(u["prompt_tokens"], u["completion_tokens"]))
        except httpx2.TimeoutException as e:
            raise GatewayError(ErrorKind.TIMEOUT, "upstream timed out", provider=self.name) from e
        except httpx2.TransportError as e:
            raise GatewayError(ErrorKind.CONNECTION, "upstream unreachable",
                               provider=self.name) from e
