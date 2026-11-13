"""Claude through the official Anthropic SDK (`pip install anthropic`, imported lazily so the
offline lab needs no packages).

Two harness-relevant rules for current Claude models:
  * pass assistant content blocks back unchanged (thinking blocks included): we keep them in `raw`
  * history must be append-only: the harness never edits old turns; it restarts a fresh context
    from the progress file instead
"""
from __future__ import annotations

from .base import ModelError, ModelReply, ToolCall

# $ per million tokens (input, output), Claude API first-party rates as of 2026-11
PRICES = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}


class ClaudeModel:
    def __init__(self, model: str = "claude-opus-5-5", effort: str = "medium", max_tokens: int = 16000):
        try:
            import anthropic
        except ImportError as e:
            raise ModelError("pip install anthropic (or run with `uv run --with anthropic`)", transient=False) from e
        self._anthropic = anthropic
        self.client = anthropic.Anthropic(max_retries=0)   # the harness owns retries and backoff
        self.model = model
        self.name = f"claude:{model}"
        self.effort = effort
        self.max_tokens = max_tokens

    def _convert(self, messages: list[dict]) -> list[dict]:
        out: list[dict] = []
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                if m.get("raw", {}) and m["raw"].get("provider") == "claude":
                    out.append({"role": "assistant", "content": m["raw"]["content"]})
                else:   # history written by another model: rebuild plain blocks
                    blocks = [{"type": "text", "text": m["content"]}] if m.get("content") else []
                    blocks += [{"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["args"]}
                               for c in m.get("tool_calls", [])]
                    out.append({"role": "assistant", "content": blocks})
            elif m["role"] == "tool":
                block = {"type": "tool_result", "tool_use_id": m["call_id"], "content": m["content"],
                         "is_error": bool(m.get("is_error"))}
                # all results for one assistant turn go back in ONE user message
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
        return out

    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> ModelReply:
        a = self._anthropic
        try:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                thinking={"type": "adaptive"},
                output_config={"effort": self.effort},
                tools=[{"name": t["name"], "description": t["description"], "input_schema": t["parameters"]}
                       for t in tools],
                messages=self._convert(messages),
            )
        except a.RateLimitError as e:
            raise ModelError(f"429: {e}", transient=True) from e
        except a.APIStatusError as e:
            raise ModelError(f"HTTP {e.status_code}: {e}", transient=e.status_code >= 500) from e
        except a.APIConnectionError as e:
            raise ModelError(f"network: {e}", transient=True) from e
        if resp.stop_reason == "refusal":
            raise ModelError("model refused (stop_reason=refusal)", transient=False)
        text = "".join(b.text for b in resp.content if b.type == "text")
        calls = [ToolCall(b.id, b.name, b.input) for b in resp.content if b.type == "tool_use"]
        pin, pout = PRICES.get(self.model, (None, None))
        cost = None if pin is None else (resp.usage.input_tokens * pin + resp.usage.output_tokens * pout) / 1e6
        return ModelReply(text=text, tool_calls=calls, input_tokens=resp.usage.input_tokens,
                          output_tokens=resp.usage.output_tokens, cost_usd=cost,
                          raw={"provider": "claude", "content": [b.model_dump(exclude_none=True) for b in resp.content]})
