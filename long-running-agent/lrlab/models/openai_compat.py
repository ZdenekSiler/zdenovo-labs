"""One adapter for every provider that speaks the OpenAI chat-completions shape.

Free tiers (limits change often; check your own console, numbers in the article are as checked
on the date it states):
  groq        https://api.groq.com/openai/v1                          GROQ_API_KEY
  openrouter  https://openrouter.ai/api/v1   (model ids ending ':free') OPENROUTER_API_KEY
  gemini      https://generativelanguage.googleapis.com/v1beta/openai  GEMINI_API_KEY
Paid:
  openai      https://api.openai.com/v1                               OPENAI_API_KEY
"""
from __future__ import annotations

import json
import os

from .base import ModelError, ModelReply, ToolCall, parse_args, post_json

PROVIDERS = {
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/openai", "GEMINI_API_KEY"),
    "openai": ("https://api.openai.com/v1", "OPENAI_API_KEY"),
}


class OpenAICompatModel:
    def __init__(self, provider: str, model: str, timeout: float = 300):
        base, key_env = PROVIDERS[provider]
        key = os.environ.get(key_env)
        if not key:
            raise ModelError(f"{key_env} not set", transient=False)
        self.name = f"{provider}:{model}"
        self.url = base + "/chat/completions"
        self.model = model
        self.headers = {"Authorization": f"Bearer {key}"}
        self.timeout = timeout
        # we only claim a price where we know it; free tiers are free until they are not
        self.free = provider in ("groq", "gemini") or model.endswith(":free")

    def _convert(self, system: str, messages: list[dict]) -> list[dict]:
        out = [{"role": "system", "content": system}]
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                msg = {"role": "assistant", "content": m.get("content") or None}
                if m.get("tool_calls"):
                    msg["tool_calls"] = [{"id": c["id"], "type": "function",
                                          "function": {"name": c["name"], "arguments": json.dumps(c["args"])}}
                                         for c in m["tool_calls"]]
                out.append(msg)
            elif m["role"] == "tool":
                out.append({"role": "tool", "tool_call_id": m["call_id"], "content": m["content"]})
        return out

    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> ModelReply:
        body = {"model": self.model, "messages": self._convert(system, messages),
                "tools": [{"type": "function", "function": t} for t in tools], "tool_choice": "auto"}
        data = post_json(self.url, body, self.headers, timeout=self.timeout)
        if "choices" not in data:
            raise ModelError(f"unexpected response: {str(data)[:300]}", transient=False)
        msg = data["choices"][0]["message"]
        calls = [ToolCall(c["id"], c["function"]["name"], parse_args(c["function"].get("arguments")))
                 for c in msg.get("tool_calls") or []]
        usage = data.get("usage") or {}
        return ModelReply(text=msg.get("content") or "", tool_calls=calls,
                          input_tokens=usage.get("prompt_tokens", 0), output_tokens=usage.get("completion_tokens", 0),
                          cost_usd=0.0 if self.free else None)
