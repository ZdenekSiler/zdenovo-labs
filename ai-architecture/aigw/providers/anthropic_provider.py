"""Claude adapter, built on the official SDK (`anthropic` 1.x, which runs on httpx2)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import anthropic

from ..types import ChatRequest, ChatResponse, ErrorKind, GatewayError, StreamChunk, Usage

FINISH = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length",
          "tool_use": "tool_call", "refusal": "refusal", "pause_turn": "stop"}


def _usage(u) -> Usage:
    # Anthropic's input_tokens excludes cache reads/writes. Normalise to OTel semantics.
    read = u.cache_read_input_tokens or 0
    write = u.cache_creation_input_tokens or 0
    return Usage(input_tokens=u.input_tokens + read + write, output_tokens=u.output_tokens,
                 cache_read_tokens=read, cache_write_tokens=write)


BODY_KIND = {"overloaded_error": ErrorKind.OVERLOADED, "rate_limit_error": ErrorKind.RATE_LIMITED,
             "api_error": ErrorKind.UPSTREAM}


def _body_type(e: anthropic.APIStatusError) -> str | None:
    body = e.body if isinstance(e.body, dict) else {}
    return (body.get("error") or {}).get("type")


def _map_error(e: Exception) -> GatewayError:
    p = "anthropic"
    match e:
        case anthropic.APITimeoutError():  # subclass of APIConnectionError: check it first
            return GatewayError(ErrorKind.TIMEOUT, "upstream timed out", provider=p)
        case anthropic.APIConnectionError():
            return GatewayError(ErrorKind.CONNECTION, "upstream unreachable", provider=p)
        case anthropic.RateLimitError():
            ra = e.response.headers.get("retry-after")
            return GatewayError(ErrorKind.RATE_LIMITED, "upstream rate limit", provider=p,
                                retry_after=float(ra) if ra else None)
        case anthropic.OverloadedError() | anthropic.ServiceUnavailableError():
            return GatewayError(ErrorKind.OVERLOADED, "upstream overloaded", provider=p)
        case anthropic.AuthenticationError() | anthropic.PermissionDeniedError():
            # The *gateway's* credential is bad. Callers can't fix that: page someone.
            return GatewayError(ErrorKind.UPSTREAM, "upstream credential rejected", provider=p)
        case anthropic.BadRequestError() if "prompt is too long" in str(e.message):
            return GatewayError(ErrorKind.CONTEXT_TOO_LONG, "context window exceeded", provider=p)
        case anthropic.BadRequestError() | anthropic.UnprocessableEntityError():
            return GatewayError(ErrorKind.INVALID_REQUEST, "upstream rejected request", provider=p)
        case anthropic.APIStatusError() if e.status_code < 400 and _body_type(e) in BODY_KIND:
            # Mid-stream failures arrive as an SSE `error` event on a 200 response: the SDK
            # raises a plain APIStatusError with status_code == 200. Classify by body type.
            return GatewayError(BODY_KIND[_body_type(e)], f"upstream {_body_type(e)} mid-stream",
                                provider=p)
        case anthropic.APIStatusError() if e.status_code >= 500:
            return GatewayError(ErrorKind.UPSTREAM, f"upstream {e.status_code}", provider=p)
        case _:
            return GatewayError(ErrorKind.UPSTREAM, type(e).__name__, provider=p)


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, client: anthropic.AsyncAnthropic | None = None, sdk_retries: int = 0):
        # sdk_retries=0: the SDK retries 408/409/429/5xx twice by default. Leave that on and
        # wrap it in gateway retries and you get 3 x N upstream attempts per request (part 3).
        self.client = (client or anthropic.AsyncAnthropic()).with_options(max_retries=sdk_retries)

    def _params(self, req: ChatRequest, model: str) -> dict:
        params = {
            "model": model,
            "max_tokens": req.max_tokens,
            "messages": [{"role": m.role, "content": m.content} for m in req.turns],
        }
        if req.system:
            # Cache the stable prefix. A timestamp in the system prompt silently kills this.
            params["system"] = [{"type": "text", "text": req.system,
                                 "cache_control": {"type": "ephemeral"}}]
        return params

    async def complete(self, req: ChatRequest, model: str, timeout: float) -> ChatResponse:
        try:
            msg = await self.client.with_options(timeout=timeout).messages.create(
                **self._params(req, model))
        except anthropic.AnthropicError as e:
            raise _map_error(e) from e
        text = "".join(b.text for b in msg.content if b.type == "text")
        return ChatResponse(id=msg.id, provider=self.name, model=msg.model, text=text,
                            finish_reason=FINISH.get(msg.stop_reason or "", "stop"),
                            usage=_usage(msg.usage))

    async def stream(self, req: ChatRequest, model: str, timeout: float) -> AsyncIterator[StreamChunk]:
        try:
            async with self.client.with_options(timeout=timeout).messages.stream(
                    **self._params(req, model)) as s:
                async for text in s.text_stream:
                    yield StreamChunk(text=text)
                final = await s.get_final_message()
        except anthropic.AnthropicError as e:
            raise _map_error(e) from e
        yield StreamChunk(finish_reason=FINISH.get(final.stop_reason or "", "stop"),
                          usage=_usage(final.usage))
