"""The gateway's canonical contract: what every caller sends and every provider returns.

Nothing in here knows about a specific vendor. Adapters translate to and from these types.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Literal

Role = Literal["system", "user", "assistant"]


@dataclass(frozen=True, slots=True)
class Message:
    role: Role
    content: str


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is calling. Resolved by the gateway from a virtual key, never sent by the client."""

    tenant: str
    key_id: str
    user: str | None = None
    scopes: frozenset[str] = frozenset()


@dataclass(slots=True)
class ChatRequest:
    model: str  # an alias from the catalog ("chat-default"), not a vendor model ID
    messages: list[Message]
    max_tokens: int = 1024
    temperature: float | None = None
    stream: bool = False
    metadata: dict[str, str] = field(default_factory=dict)  # feature, prompt_version, ...

    @property
    def system(self) -> str | None:
        parts = [m.content for m in self.messages if m.role == "system"]
        return "\n\n".join(parts) if parts else None

    @property
    def turns(self) -> list[Message]:
        return [m for m in self.messages if m.role != "system"]


@dataclass(frozen=True, slots=True)
class Usage:
    """Token counts in OpenTelemetry semantics: input_tokens INCLUDES cached tokens.

    Anthropic's API reports input_tokens *excluding* cache reads and writes; adapters must
    normalise, or the gateway's numbers disagree with every trace backend (part 7).
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    @property
    def uncached_input_tokens(self) -> int:
        return self.input_tokens - self.cache_read_tokens - self.cache_write_tokens


@dataclass(frozen=True, slots=True)
class ChatResponse:
    id: str
    provider: str
    model: str  # the concrete model that answered, after routing and fallback
    text: str
    finish_reason: str  # normalised: stop | length | refusal | tool_call | error
    usage: Usage
    cached: bool = False


@dataclass(frozen=True, slots=True)
class StreamChunk:
    text: str = ""
    finish_reason: str | None = None
    usage: Usage | None = None
    model: str | None = None  # set by the router on the final chunk


class ErrorKind(enum.StrEnum):
    INVALID_REQUEST = "invalid_request"
    AUTH = "auth"
    NOT_FOUND = "not_found"
    RATE_LIMITED = "rate_limited"
    OVERLOADED = "overloaded"
    UPSTREAM = "upstream_error"
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    CONTEXT_TOO_LONG = "context_too_long"
    # produced by the gateway itself, never by a provider
    BUDGET_EXCEEDED = "budget_exceeded"
    QUOTA_EXCEEDED = "quota_exceeded"
    BLOCKED = "blocked_by_guardrail"
    CIRCUIT_OPEN = "circuit_open"
    DEADLINE = "deadline_exceeded"


# Retrying these can succeed. Everything else fails the same way twice.
RETRYABLE = {ErrorKind.RATE_LIMITED, ErrorKind.OVERLOADED, ErrorKind.UPSTREAM,
             ErrorKind.TIMEOUT, ErrorKind.CONNECTION}
# Trying a *different* provider can succeed even when retrying the same one can't.
FALLBACKABLE = RETRYABLE | {ErrorKind.CIRCUIT_OPEN, ErrorKind.CONTEXT_TOO_LONG}

HTTP_STATUS = {
    ErrorKind.INVALID_REQUEST: 400, ErrorKind.AUTH: 401, ErrorKind.NOT_FOUND: 404,
    ErrorKind.CONTEXT_TOO_LONG: 400, ErrorKind.BLOCKED: 422, ErrorKind.RATE_LIMITED: 429,
    ErrorKind.QUOTA_EXCEEDED: 429, ErrorKind.BUDGET_EXCEEDED: 402, ErrorKind.OVERLOADED: 503,
    ErrorKind.CIRCUIT_OPEN: 503, ErrorKind.UPSTREAM: 502, ErrorKind.CONNECTION: 502,
    ErrorKind.TIMEOUT: 504, ErrorKind.DEADLINE: 504,
}


class GatewayError(Exception):
    def __init__(self, kind: ErrorKind, message: str, *, provider: str | None = None,
                 retry_after: float | None = None):
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.message = message
        self.provider = provider
        self.retry_after = retry_after

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE

    @property
    def status(self) -> int:
        return HTTP_STATUS[self.kind]

    def to_body(self) -> dict:
        # Never leak upstream bodies or provider names to callers: they reveal topology.
        return {"error": {"type": self.kind.value, "message": self.message}}
