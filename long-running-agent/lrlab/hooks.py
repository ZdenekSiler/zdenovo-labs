"""Permissions and hooks: code that runs before and after every tool call.

Pre-tool hooks decide (allow / deny with a reason the model can read).
Post-tool hooks transform the result before the model sees it.

These are plain Python, deterministic and cheap. None of them asks a model whether something
is safe, because a model can be talked out of its answer.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .tools import ToolSpec


@dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str = ""


@dataclass(frozen=True)
class RunContext:
    conv_id: str
    provenance: str          # of the message that started this run: operator | user | untrusted


class PermissionPolicy:
    """Which permission classes each provenance may use. An untrusted email can read and write
    notes; it cannot reach the network or run commands. A scheduled job gets no shell."""

    DEFAULT = {
        "operator": {"read", "write", "network", "human"},
        "user": {"read", "write", "network", "human"},
        "untrusted": {"read", "write", "human"},
    }

    def __init__(self, table: dict[str, set[str]] | None = None):
        self.table = table or self.DEFAULT

    def check(self, call: dict, spec: ToolSpec, ctx: RunContext) -> Decision:
        allowed = self.table.get(ctx.provenance, set())
        if spec.permission not in allowed:
            return Decision(False, f"'{spec.name}' needs {spec.permission!r} permission, which "
                                   f"{ctx.provenance} runs do not have. Use another tool.")
        return Decision(True)


class SendToUserGuard:
    """Structural rules for the talking tool, so a bad message never reaches a phone."""

    def check(self, call: dict, spec: ToolSpec, ctx: RunContext) -> Decision:
        if spec.name != "send_to_user":
            return Decision(True)
        a = call["args"]
        if not a.get("text", "").strip():
            return Decision(False, "text must not be empty: it is the only thing the user will read")
        if a["kind"] == "choice" and len(a.get("options") or []) < 2:
            return Decision(False, "kind='choice' needs at least two options")
        if a["kind"] == "file" and not a.get("path"):
            return Decision(False, "kind='file' needs a path")
        if a["kind"] == "secret" and not re.fullmatch(r"[a-z0-9_]{3,40}", a.get("secret_name") or ""):
            return Decision(False, "kind='secret' needs secret_name like 'github_token'")
        if a["kind"] == "secret" and a.get("final"):
            return Decision(False, "a secret request cannot be final; you need the answer")
        return Decision(True)


class Redactor:
    """Post-tool: strip things that should never enter a transcript (and so never a log, a
    provider's retention window, or a message to the user)."""

    PATTERNS = [
        (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "<email>"),
        (re.compile(r"sk-[A-Za-z0-9_-]{16,}"), "<api-key>"),
        (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "<github-token>"),
    ]

    def __init__(self):
        self.count = 0

    def transform(self, call: dict, spec: ToolSpec, text: str) -> str:
        for pat, repl in self.PATTERNS:
            text, n = pat.subn(repl, text)
            self.count += n
        return text


class ProvenanceTagger:
    """Post-tool: anything that came from the network is wrapped and labelled as data."""

    def transform(self, call: dict, spec: ToolSpec, text: str) -> str:
        if spec.permission != "network":
            return text
        src = call["args"].get("url") or f"pypi:{call['args'].get('package', '')}"
        return f'<untrusted_data source="{src}">\n{text}\n</untrusted_data>'


class OutputCap:
    def __init__(self, max_chars: int = 6000):
        self.max_chars = max_chars

    def transform(self, call: dict, spec: ToolSpec, text: str) -> str:
        if len(text) <= self.max_chars:
            return text
        return text[: self.max_chars] + f"\n[truncated {len(text) - self.max_chars} chars]"


def render_result(result: dict) -> str:
    """Tool results go to the model as compact JSON text."""
    return json.dumps(result, ensure_ascii=False, separators=(",", ":"))
