"""Build a token-sized haystack with a needle at a known depth.

The point of the class split: a haystack is three independent decisions that
people usually hard-code into one script and then cannot vary.

  Corpus   - where the filler prose comes from
  Needle   - the fact to retrieve, and how it is graded
  Haystack - how big, and where in the pile the needle lands

Filler is built up paragraph by paragraph until the token budget is hit, so the
size is measured, not assumed.
"""
from __future__ import annotations

import random
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from counters import TokenCounter

# ─── Corpora ──────────────────────────────────────────────────────────────────


class Corpus:
    """A deterministic, shuffled pool of filler paragraphs."""

    def __init__(self, paragraphs: list[str], seed: int = 1337) -> None:
        usable = [p.strip() for p in paragraphs if len(p.strip()) > 120]
        if not usable:
            raise ValueError("corpus has no paragraphs over 120 chars")
        self._paragraphs = usable
        self._seed = seed

    def stream(self) -> list[str]:
        """Shuffled copy — same order for the same seed, so runs are comparable."""
        pool = list(self._paragraphs)
        random.Random(self._seed).shuffle(pool)
        return pool

    @classmethod
    def from_dir(cls, path: Path, seed: int = 1337) -> "Corpus":
        chunks: list[str] = []
        for file in sorted(path.rglob("*.md")):
            chunks.extend(_split_paragraphs(file.read_text(encoding="utf-8")))
        return cls(chunks, seed)

    @classmethod
    def from_blog_db(cls, db_path: Path, seed: int = 1337) -> "Corpus":
        """Filler from your own published posts. The distractors are then real."""
        conn = sqlite3.connect(db_path)
        try:
            rows = conn.execute("SELECT content FROM posts").fetchall()
        finally:
            conn.close()
        chunks: list[str] = []
        for (content,) in rows:
            chunks.extend(_split_paragraphs(content))
        return cls(chunks, seed)


def _split_paragraphs(text: str) -> list[str]:
    """Paragraphs only — code fences and tables would make the filler too easy
    to tell apart from the needle, which is the one thing we are measuring."""
    without_code = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    return [p for p in re.split(r"\n\s*\n", without_code) if not p.lstrip().startswith(("|", "#", ">", "-"))]


# ─── Needle ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Needle:
    """A planted fact, the question that retrieves it, and the grader."""

    fact: str
    question: str
    answer: str

    def grade(self, response: str) -> bool:
        return self.answer.lower() in response.lower()


DEFAULT_NEEDLE = Needle(
    fact="Internal note: the staging deploy key for the Oslo region rotates every 41 days.",
    question="How often does the staging deploy key for the Oslo region rotate? Answer with just the number of days.",
    answer="41",
)


# ─── Haystack ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BuiltHaystack:
    text: str
    tokens: int
    depth: float
    needle_token_offset: int


class Haystack:
    def __init__(self, corpus: Corpus, counter: TokenCounter) -> None:
        self._corpus = corpus
        self._counter = counter

    def build(
        self,
        target_tokens: int,
        needle: Needle,
        depth: float,
        plant: Callable[[list[str]], list[str]] | None = None,
    ) -> BuiltHaystack:
        """Fill to target_tokens, then splice the needle in at `depth` (0.0 = first
        paragraph, 1.0 = last). Depth is measured in tokens, not paragraphs — a
        paragraph index means nothing when paragraph lengths vary 10x.

        `plant` runs on the filler before the needle goes in; that is the hook the
        distractor set uses, so near-misses are part of the pile rather than
        appended to it."""
        if not 0.0 <= depth <= 1.0:
            raise ValueError("depth must be between 0.0 and 1.0")

        paragraphs: list[str] = []
        running = 0
        pool = self._corpus.stream()
        i = 0
        while running < target_tokens:
            para = pool[i % len(pool)]
            i += 1
            paragraphs.append(para)
            running += self._counter.count(para + "\n\n")

        if plant is not None:
            paragraphs = plant(paragraphs)

        cut_at = self._insert_index(paragraphs, depth)
        before = paragraphs[:cut_at]
        offset = sum(self._counter.count(p + "\n\n") for p in before)
        spliced = [*before, needle.fact, *paragraphs[cut_at:]]
        text = "\n\n".join(spliced)
        return BuiltHaystack(
            text=text,
            tokens=self._counter.count(text),
            depth=depth,
            needle_token_offset=offset,
        )

    def _insert_index(self, paragraphs: list[str], depth: float) -> int:
        total = sum(self._counter.count(p + "\n\n") for p in paragraphs)
        want = total * depth
        running = 0
        for idx, para in enumerate(paragraphs):
            if running >= want:
                return idx
            running += self._counter.count(para + "\n\n")
        return len(paragraphs)
