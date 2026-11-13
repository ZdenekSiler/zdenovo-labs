"""Local models through Ollama's native /api/chat (tool calling supported on tool-capable models)."""
from __future__ import annotations

import os

from .base import ModelReply, ToolCall, parse_args, post_json


class OllamaModel:
    def __init__(self, model: str, host: str | None = None, timeout: float = 600):
        self.model = model
        self.name = f"ollama:{model}"
        self.host = (host or os.environ.get("OLLAMA_HOST", "http://localhost:11434")).rstrip("/")
        self.timeout = timeout
        self._n = 0

    def _convert(self, system: str, messages: list[dict]) -> list[dict]:
        out = [{"role": "system", "content": system}]
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                msg = {"role": "assistant", "content": m.get("content") or ""}
                if m.get("tool_calls"):
                    msg["tool_calls"] = [{"type": "function", "function": {"name": c["name"], "arguments": c["args"]}}
                                         for c in m["tool_calls"]]
                out.append(msg)
            elif m["role"] == "tool":
                out.append({"role": "tool", "tool_name": m["name"], "content": m["content"]})
        return out

    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> ModelReply:
        body = {
            "model": self.model,
            "messages": self._convert(system, messages),
            "tools": [{"type": "function", "function": t} for t in tools],
            "stream": False,
            "options": {"temperature": 0, "num_ctx": 16384},
        }
        data = post_json(f"{self.host}/api/chat", body, {}, timeout=self.timeout)
        msg = data.get("message", {})
        calls = []
        for c in msg.get("tool_calls") or []:
            self._n += 1
            fn = c.get("function", {})
            calls.append(ToolCall(f"ol_{self._n}", fn.get("name", ""), parse_args(fn.get("arguments"))))
        return ModelReply(text=msg.get("content", ""), tool_calls=calls,
                          input_tokens=data.get("prompt_eval_count", 0),
                          output_tokens=data.get("eval_count", 0), cost_usd=0.0)
