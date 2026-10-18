"""Token counters for the context-rot lab.

Two implementations behind one interface:

  ApiTokenCounter  - the only honest one. Calls /v1/messages/count_tokens, which is
                     model-specific and free. Never use tiktoken for Claude: it is
                     OpenAI's tokenizer and undercounts Claude by ~15-20% on prose
                     and much more on code.
  RoughTokenCounter - chars/3.7, for offline dry-runs of the harness only. Good
                     enough to prove the plumbing works, not to report a number.

Both memoize, because a position sweep counts the same filler paragraphs hundreds
of times and each ApiTokenCounter miss is a round-trip.
"""
from __future__ import annotations

from typing import Protocol


class TokenCounter(Protocol):
    def count(self, text: str) -> int: ...


# ─── Implementations ──────────────────────────────────────────────────────────


class RoughTokenCounter:
    """Offline estimate. Labelled rough so nobody quotes it in a blog post."""

    def __init__(self, chars_per_token: float = 3.7) -> None:
        self._ratio = chars_per_token

    def count(self, text: str) -> int:
        return max(1, int(len(text) / self._ratio))


class ApiTokenCounter:
    """Model-specific counts from the API. Free, but rate-limited — so cache."""

    def __init__(self, client: object, model: str) -> None:
        self._client = client
        self._model = model
        self._cache: dict[str, int] = {}

    def count(self, text: str) -> int:
        if text not in self._cache:
            resp = self._client.messages.count_tokens(
                model=self._model,
                messages=[{"role": "user", "content": text}],
            )
            self._cache[text] = resp.input_tokens
        return self._cache[text]
