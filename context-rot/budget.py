"""A context budget that packs for the edges and re-pins the invariants.

Three rules, all of them consequences of the measurements in probe.py:

  1. A context window is a budget, not a container. Items get scores; the
     budget evicts from the bottom until it fits.
  2. Position may be a resource. The best evidence goes at the two edges and
     the weakest survivor sinks to the middle. This one is cheap insurance, not
     a law: the middle-of-context penalty is strong in some task shapes and
     absent in others, so run a depth sweep before you trust it. Delete
     _edge_order if yours comes back flat.
  3. The rules you cannot afford to have ignored get re-stated at the tail,
     after the evidence, because that is the other edge the model reads.

The prefix is kept byte-stable so prompt caching still hits: stable system text
first, volatile packed evidence after the cache breakpoint.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from counters import TokenCounter

# ─── Items ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Item:
    """A candidate chunk. `score` is whatever your retriever believes; the
    budget only needs it to be comparable."""

    key: str
    text: str
    score: float


@dataclass
class PackResult:
    blocks: list[dict]
    used_tokens: int
    kept: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)

    @property
    def prompt(self) -> str:
        return "\n\n".join(b["text"] for b in self.blocks)


# ─── Budget ───────────────────────────────────────────────────────────────────


class ContextBudget:
    """Packs items under a hard token budget, edges first.

    >>> budget = ContextBudget(RoughTokenCounter(), total_tokens=400, reserve_for_output=100)
    >>> budget.pin("Never invent a slug.")
    >>> for i in range(10):
    ...     budget.add(Item(f"c{i}", "chunk " * 30, score=1.0 / (i + 1)))
    >>> result = budget.pack()
    >>> result.used_tokens <= 300
    True
    >>> result.kept[0], result.kept[-1]  # best evidence at both edges
    ('c0', 'c1')
    >>> result.dropped  # lowest-scoring items evicted, not truncated mid-chunk
    ['c6', 'c7', 'c8', 'c9']
    """

    def __init__(
        self,
        counter: TokenCounter,
        total_tokens: int,
        stable_system: str = "",
        reserve_for_output: int = 4096,
    ) -> None:
        self._counter = counter
        self._budget = total_tokens - reserve_for_output
        self._stable_system = stable_system
        self._pinned: list[str] = []
        self._items: list[Item] = []
        if self._budget <= 0:
            raise ValueError("reserve_for_output leaves no room for input")

    def pin(self, text: str) -> None:
        """An invariant. Costs budget, never gets evicted, and is restated at the
        tail of the prompt where the model is still reading."""
        self._pinned.append(text)

    def add(self, item: Item) -> None:
        self._items.append(item)

    # ── packing ──

    def pack(self) -> PackResult:
        fixed = self._counter.count(self._stable_system) if self._stable_system else 0
        fixed += sum(self._counter.count(p) for p in self._pinned) * 2  # head + tail copy

        remaining = self._budget - fixed
        if remaining <= 0:
            raise ValueError("pinned content alone exceeds the budget")

        kept: list[Item] = []
        dropped: list[str] = []
        for item in sorted(self._items, key=lambda i: i.score, reverse=True):
            cost = self._counter.count(item.text)
            if cost <= remaining:
                kept.append(item)
                remaining -= cost
            else:
                dropped.append(item.key)

        ordered = self._edge_order(kept)
        blocks = self._render(ordered)
        used = self._budget - remaining
        return PackResult(
            blocks=blocks,
            used_tokens=used,
            kept=[i.key for i in ordered],
            dropped=dropped,
        )

    @staticmethod
    def _edge_order(items: list[Item]) -> list[Item]:
        """Best item first, second-best last, third-best second, and so on, so
        relevance decays towards the middle instead of towards the end."""
        head: list[Item] = []
        tail: list[Item] = []
        for n, item in enumerate(items):  # items arrive score-descending
            (head if n % 2 == 0 else tail).append(item)
        return head + list(reversed(tail))

    def _render(self, items: list[Item]) -> list[dict]:
        rules = "\n".join(f"- {p}" for p in self._pinned)
        blocks: list[dict] = []
        if self._stable_system:
            # Stable prefix: everything before the breakpoint must be byte-identical
            # across requests or the cache read is a cache write.
            blocks.append(
                {
                    "type": "text",
                    "text": self._stable_system + ("\n\nRules:\n" + rules if rules else ""),
                    "cache_control": {"type": "ephemeral"},
                }
            )
        body = "\n\n".join(f"<chunk id=\"{i.key}\">\n{i.text}\n</chunk>" for i in items)
        blocks.append({"type": "text", "text": f"<evidence>\n{body}\n</evidence>"})
        if rules:
            blocks.append({"type": "text", "text": f"Reminder — the rules above still apply:\n{rules}"})
        return blocks
