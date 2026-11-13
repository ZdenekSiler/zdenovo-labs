"""The model is a swappable box. One protocol, one provider-neutral message format.

History format (what the harness checkpoints):
  {"role": "user", "content": str}
  {"role": "assistant", "content": str, "tool_calls": [{"id", "name", "args"}], "raw": {...} | None}
  {"role": "tool", "call_id": str, "name": str, "content": str, "is_error": bool}

`raw` keeps a provider's own assistant blocks so they can be replayed byte-for-byte. Claude needs
that: thinking blocks must go back unchanged, and history must be append-only.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolCall:
    id: str
    name: str
    args: Any                 # usually a dict; small models sometimes send a string. The harness checks.


@dataclass
class ModelReply:
    text: str
    tool_calls: list[ToolCall]
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = 0.0      # None = provider price unknown to this lab
    raw: dict | None = None


class ModelError(Exception):
    def __init__(self, msg: str, transient: bool, retry_after: float | None = None):
        super().__init__(msg)
        self.transient = transient
        self.retry_after = retry_after


class ModelClient(Protocol):
    name: str

    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> ModelReply: ...


def estimate_tokens(messages: list[dict]) -> int:
    """~4 chars per token. Good enough to decide when to restart a context; not for billing."""
    return sum(len(json.dumps(m, ensure_ascii=False)) for m in messages) // 4


def post_json(url: str, body: dict, headers: dict[str, str], timeout: float = 300) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400]
        ra = e.headers.get("retry-after")
        raise ModelError(f"HTTP {e.code}: {detail}", transient=e.code == 429 or e.code >= 500,
                         retry_after=float(ra) if ra and ra.replace(".", "").isdigit() else None) from e
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise ModelError(f"network: {e}", transient=True) from e


def parse_args(raw: Any) -> Any:
    """OpenAI-style APIs send arguments as a JSON string; Ollama sends an object."""
    if isinstance(raw, str):
        try:
            return json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            return raw            # let the harness report it back as an invalid call
    return raw
