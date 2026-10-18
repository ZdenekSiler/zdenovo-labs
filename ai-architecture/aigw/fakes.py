"""Scripted fake upstreams behind httpx2.MockTransport.

They speak the real wire formats (Anthropic Messages JSON + SSE, chat-completions JSON + SSE),
so the official SDK parses them exactly as it would production traffic. Every transcript in
the series that says [fake upstream] came from these, not from a live model.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field

import anthropic
import httpx2

Responder = Callable[[str], str]  # last user message -> reply text


def _default_reply(prompt: str) -> str:
    return f"Reply to: {prompt[:160]}"


def _tokens(text: str) -> int:
    return max(1, len(text) // 4)


@dataclass
class FakeUpstream:
    """script: one entry per call, consumed in order; when it runs out, every call is 'ok'.

    Entries: 'ok', 'slow:<seconds>', 'timeout', 'connect', 'midstream' (Anthropic streams only:
    a 200 that dies after the first delta), or an HTTP status like 429/529/500.
    """

    kind: str  # "anthropic" | "selfhosted"
    script: list = field(default_factory=list)
    responder: Responder = _default_reply
    min_cache_tokens: int = 1024  # real minimums are model-dependent (512-4096)
    calls: int = 0
    log: list[str] = field(default_factory=list)
    seen: list[str] = field(default_factory=list)  # last user message of every OK call
    _cache: set[str] = field(default_factory=set)
    _midstream: bool = False

    def _next(self) -> str:
        self.calls += 1
        step = self.script.pop(0) if self.script else "ok"
        self.log.append(str(step))
        return str(step)

    async def handler(self, request: httpx2.Request) -> httpx2.Response:
        step = self._next()
        if step.startswith("slow:"):
            await asyncio.sleep(float(step[5:]))
        elif step == "timeout":
            raise httpx2.ReadTimeout("fake read timeout", request=request)
        elif step == "connect":
            raise httpx2.ConnectError("fake connection refused", request=request)
        elif step.isdigit():
            return self._error(int(step))
        self._midstream = step == "midstream"
        body = json.loads(request.content)
        self.seen.append(body["messages"][-1]["content"])
        return self._anthropic(body) if self.kind == "anthropic" else self._chat(body)

    # --- errors -------------------------------------------------------------------------
    def _error(self, status: int) -> httpx2.Response:
        headers = {"retry-after": "1"} if status == 429 else {}
        if self.kind == "anthropic":
            etype = {429: "rate_limit_error", 529: "overloaded_error",
                     400: "invalid_request_error"}.get(status, "api_error")
            return httpx2.Response(status, headers=headers, json={
                "type": "error", "error": {"type": etype, "message": f"fake {status}"}})
        return httpx2.Response(status, headers=headers, json={"error": {"message": f"fake {status}"}})

    # --- Anthropic Messages API ---------------------------------------------------------
    def _anthropic(self, body: dict) -> httpx2.Response:
        system = "".join(b["text"] for b in body.get("system", []))
        prompt = body["messages"][-1]["content"]
        reply = self.responder(prompt)
        sys_tokens = _tokens(system) if system else 0
        rest = sum(_tokens(m["content"]) for m in body["messages"])
        read = write = 0
        if sys_tokens >= self.min_cache_tokens:
            key = hashlib.sha256((body["model"] + system).encode()).hexdigest()
            if key in self._cache:
                read = sys_tokens
            else:
                write = sys_tokens
                self._cache.add(key)
        usage = {"input_tokens": rest + sys_tokens - read - write, "output_tokens": _tokens(reply),
                 "cache_read_input_tokens": read, "cache_creation_input_tokens": write}
        msg_id = f"msg_fake_{self.calls:03d}"
        if not body.get("stream"):
            return httpx2.Response(200, headers={"request-id": f"req_fake_{self.calls:03d}"}, json={
                "id": msg_id, "type": "message", "role": "assistant", "model": body["model"],
                "content": [{"type": "text", "text": reply}], "stop_reason": "end_turn",
                "stop_sequence": None, "usage": usage})
        events = [("message_start", {"type": "message_start", "message": {
            "id": msg_id, "type": "message", "role": "assistant", "model": body["model"],
            "content": [], "stop_reason": None, "stop_sequence": None,
            "usage": {**usage, "output_tokens": 1}}}),
            ("content_block_start", {"type": "content_block_start", "index": 0,
                                     "content_block": {"type": "text", "text": ""}})]
        for piece in _pieces(reply):
            events.append(("content_block_delta", {"type": "content_block_delta", "index": 0,
                                                   "delta": {"type": "text_delta", "text": piece}}))
        events += [("content_block_stop", {"type": "content_block_stop", "index": 0}),
                   ("message_delta", {"type": "message_delta",
                                      "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                                      "usage": {"output_tokens": usage["output_tokens"]}}),
                   ("message_stop", {"type": "message_stop"})]
        if self._midstream:
            # Status 200 is long gone; the failure arrives as an SSE error event.
            events = events[:3] + [("error", {"type": "error", "error": {
                "type": "overloaded_error", "message": "fake mid-stream overload"}})]
        sse = "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events)
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=sse.encode())

    # --- chat-completions (self-hosted) -------------------------------------------------
    def _chat(self, body: dict) -> httpx2.Response:
        prompt = body["messages"][-1]["content"]
        reply = self.responder(prompt)
        pt = sum(_tokens(m["content"]) for m in body["messages"])
        usage = {"prompt_tokens": pt, "completion_tokens": _tokens(reply), "total_tokens": 0}
        if not body.get("stream"):
            return httpx2.Response(200, json={
                "id": f"chatcmpl-fake-{self.calls:03d}", "model": body["model"],
                "choices": [{"index": 0, "message": {"role": "assistant", "content": reply},
                             "finish_reason": "stop"}], "usage": usage})
        lines = [{"choices": [{"index": 0, "delta": {"content": p}, "finish_reason": None}]}
                 for p in _pieces(reply)]
        lines.append({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        lines.append({"choices": [], "usage": usage})
        sse = "".join(f"data: {json.dumps(x)}\n\n" for x in lines) + "data: [DONE]\n\n"
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=sse.encode())


def _pieces(text: str, size: int = 12) -> list[str]:
    return [text[i:i + size] for i in range(0, len(text), size)]


def anthropic_client(fake: FakeUpstream, **kw) -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(
        api_key="sk-ant-fake", http_client=anthropic.DefaultAsyncHttpxClient(
            transport=httpx2.MockTransport(fake.handler)), **kw)


def selfhosted_client(fake: FakeUpstream) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.MockTransport(fake.handler))
