"""Response caches. Exact-match is boring and safe; semantic is exciting and dangerous."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter, OrderedDict
from dataclasses import dataclass

from .resilience import Clock
from .types import ChatRequest, ChatResponse


def cache_key(tenant: str, alias: str, req: ChatRequest) -> str:
    # Tenant FIRST. A cache keyed on the prompt alone serves one customer's answer to another.
    payload = {"tenant": tenant, "alias": alias, "max_tokens": req.max_tokens,
               "temperature": req.temperature, "prompt_version": req.metadata.get("prompt_version"),
               "messages": [[m.role, m.content] for m in req.messages]}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


class ExactCache:
    """LRU + TTL. Opt-in per request: only deterministic-enough, non-personal calls belong here."""

    def __init__(self, clock: Clock, ttl: float = 3600, max_items: int = 10_000):
        self.clock, self.ttl, self.max_items = clock, ttl, max_items
        self._items: OrderedDict[str, tuple[float, ChatResponse]] = OrderedDict()
        self.hits = self.misses = 0

    def get(self, key: str) -> ChatResponse | None:
        item = self._items.get(key)
        if item is None or self.clock.now() - item[0] > self.ttl:
            self._items.pop(key, None)
            self.misses += 1
            return None
        self._items.move_to_end(key)
        self.hits += 1
        return item[1]

    def put(self, key: str, resp: ChatResponse) -> None:
        if resp.finish_reason != "stop":
            return  # never cache truncated, refused or errored answers
        self._items[key] = (self.clock.now(), resp)
        self._items.move_to_end(key)
        while len(self._items) > self.max_items:
            self._items.popitem(last=False)


# --- Semantic cache ---------------------------------------------------------------------
# TOY embedding: a bag of words. Real embedding models are far better, but they share the
# failure this demo exists to show: "cancel" and "don't cancel" land close together.

def toy_embed(text: str) -> Counter:
    return Counter(re.findall(r"[a-z']+", text.lower()))


def cosine(a: Counter, b: Counter) -> float:
    dot = sum(a[k] * b[k] for k in a)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


@dataclass
class SemanticHit:
    similarity: float
    cached_prompt: str
    response: ChatResponse


class SemanticCache:
    def __init__(self, threshold: float = 0.85):
        self.threshold = threshold
        self._rows: dict[str, list[tuple[Counter, str, ChatResponse]]] = {}

    def get(self, tenant: str, prompt: str) -> SemanticHit | None:
        q = toy_embed(prompt)
        best = None
        for vec, text, resp in self._rows.get(tenant, []):  # partitioned by tenant
            sim = cosine(q, vec)
            if sim >= self.threshold and (best is None or sim > best.similarity):
                best = SemanticHit(sim, text, resp)
        return best

    def put(self, tenant: str, prompt: str, resp: ChatResponse) -> None:
        self._rows.setdefault(tenant, []).append((toy_embed(prompt), prompt, resp))
