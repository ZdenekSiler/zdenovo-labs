"""HTTP front for the gateway. Thin on purpose: all policy lives in Gateway, not in routes."""

from __future__ import annotations

import json

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .gateway import Gateway
from .types import ChatRequest, GatewayError, Message


class MessageIn(BaseModel):
    role: str = Field(pattern="^(system|user|assistant)$")
    content: str = Field(max_length=200_000)


class ChatIn(BaseModel):
    model: str
    messages: list[MessageIn] = Field(min_length=1, max_length=500)
    max_tokens: int = Field(1024, ge=1, le=128_000)
    temperature: float | None = Field(None, ge=0, le=1)
    stream: bool = False
    metadata: dict[str, str] = Field(default_factory=dict)


def create_app(gateway: Gateway) -> FastAPI:
    app = FastAPI(title="aigw")

    @app.exception_handler(GatewayError)
    async def gateway_error(_: Request, e: GatewayError) -> JSONResponse:
        headers = {"retry-after": str(int(e.retry_after) + 1)} if e.retry_after else {}
        return JSONResponse(e.to_body(), status_code=e.status, headers=headers)

    @app.post("/v1/chat")
    async def chat(body: ChatIn, authorization: str | None = Header(None)):
        key = authorization.removeprefix("Bearer ").strip() if authorization else None
        req = ChatRequest(model=body.model, messages=[Message(m.role, m.content) for m in body.messages],
                          max_tokens=body.max_tokens, temperature=body.temperature,
                          metadata=body.metadata)
        if body.stream:
            async def sse():
                try:
                    async for text in gateway.stream(key, req):
                        yield f"data: {json.dumps({'text': text})}\n\n"
                except GatewayError as e:  # headers are already sent: report in-band
                    yield f"data: {json.dumps(e.to_body())}\n\n"
                yield "data: [DONE]\n\n"
            return StreamingResponse(sse(), media_type="text/event-stream")
        r = await gateway.chat(key, req)
        return {"id": r.response.id, "model": r.response.model, "text": r.response.text,
                "finish_reason": r.response.finish_reason, "cached": r.response.cached,
                "usage": {"input_tokens": r.response.usage.input_tokens,
                          "output_tokens": r.response.usage.output_tokens,
                          "cache_read_tokens": r.response.usage.cache_read_tokens},
                "cost_usd": round(r.cost_usd, 8)}

    @app.get("/healthz")
    async def healthz():
        # Liveness only. Do NOT call providers here: a vendor outage would restart every pod.
        return {"ok": True}

    return app
