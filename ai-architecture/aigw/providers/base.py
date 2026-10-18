from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from ..types import ChatRequest, ChatResponse, StreamChunk


@runtime_checkable
class Provider(Protocol):
    """One upstream vendor or self-hosted runtime. Adapters never retry: the gateway does."""

    name: str

    async def complete(self, req: ChatRequest, model: str, timeout: float) -> ChatResponse: ...

    def stream(self, req: ChatRequest, model: str, timeout: float) -> AsyncIterator[StreamChunk]: ...
