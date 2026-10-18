#!/usr/bin/env python3
"""Measure needle recall across context length and needle depth.

    # plumbing check, no API calls, no spend
    python probe.py --offline

    # the real thing (costs money: ~tokens x length x depth x trials)
    python probe.py --lengths 8000,32000,128000 --depths 0,0.25,0.5,0.75,1 --trials 3

Grading is a local substring match, so the probe costs one output token's worth
of generation per trial and never needs an LLM judge.

Note on determinism: temperature/top_p are gone on Opus 5, Sonnet 5 and the rest
of the 4.6+ family (they return a 400). You cannot pin sampling any more, which
is exactly why `--trials` defaults above 1 — you are measuring a rate, not a
single answer.
"""
from __future__ import annotations

import argparse
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

from counters import ApiTokenCounter, RoughTokenCounter, TokenCounter
from distractors import DEFAULT_DISTRACTORS, STRICT_NEEDLE
from haystack import Corpus, Haystack, Needle

SYSTEM = (
    "Answer using only the notes provided in the user message. "
    "If the notes do not contain the answer, reply exactly: NOT FOUND."
)

# ─── Results ──────────────────────────────────────────────────────────────────


@dataclass
class Trial:
    tokens: int
    depth: float
    distractors: int
    found: bool
    response: str


class Report:
    def __init__(self, trials: list[Trial]) -> None:
        self._trials = trials

    def table(self) -> str:
        depths = sorted({t.depth for t in self._trials})
        lengths = sorted({t.tokens for t in self._trials})
        head = "| tokens | " + " | ".join(f"depth {d:.0%}" for d in depths) + " |"
        rule = "|---" * (len(depths) + 1) + "|"
        rows = []
        for length in lengths:
            cells = []
            for depth in depths:
                hits = [t.found for t in self._trials if t.tokens == length and t.depth == depth]
                cells.append(f"{statistics.mean(hits):.0%}" if hits else "-")
            rows.append(f"| {length:,} | " + " | ".join(cells) + " |")
        return "\n".join([head, rule, *rows])


# ─── Probe ────────────────────────────────────────────────────────────────────


class OfflineModel:
    """Stand-in that answers nothing, so --offline exercises the harness without
    inventing a result. Every cell comes back 0% by construction."""

    def ask(self, system: str, prompt: str) -> str:
        return "[offline: no model was called]"


class ClaudeModel:
    def __init__(self, client: object, model: str) -> None:
        self._client = client
        self._model = model

    def ask(self, system: str, prompt: str) -> str:
        resp = self._client.messages.create(
            model=self._model,
            max_tokens=64,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        if resp.stop_reason == "refusal":
            return "[refused]"
        return "".join(block.text for block in resp.content if block.type == "text")


class RecallProbe:
    def __init__(self, model: object, haystack: Haystack, trials: int = 3) -> None:
        self._model = model
        self._haystack = haystack
        self._trials = trials

    def sweep(
        self,
        lengths: list[int],
        depths: list[float],
        needle: Needle,
        distractors: int = 0,
    ) -> list[Trial]:
        plant = None
        if distractors:
            plant = lambda paras: DEFAULT_DISTRACTORS.plant(paras, distractors)  # noqa: E731

        results: list[Trial] = []
        for length in lengths:
            for depth in depths:
                built = self._haystack.build(length, needle, depth, plant=plant)
                prompt = f"<notes>\n{built.text}\n</notes>\n\n{needle.question}"
                for _ in range(self._trials):
                    answer = self._model.ask(SYSTEM, prompt)
                    results.append(
                        Trial(
                            tokens=built.tokens,
                            depth=depth,
                            distractors=distractors,
                            found=needle.grade(answer),
                            response=answer.strip()[:80],
                        )
                    )
                print(
                    f"  {built.tokens:>8,} tok  depth {depth:>5.0%}  "
                    f"needle at ~{built.needle_token_offset:,} tok",
                    file=sys.stderr,
                )
        return results


# ─── CLI ──────────────────────────────────────────────────────────────────────


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="claude-opus-5")
    ap.add_argument("--lengths", default="4000,16000,64000")
    ap.add_argument("--depths", default="0,0.5,1")
    ap.add_argument("--trials", type=int, default=3)
    ap.add_argument("--distractors", type=int, default=0)
    ap.add_argument("--corpus", type=Path, default=None, help="dir of .md files")
    ap.add_argument("--blog-db", type=Path, default=None, help="SQLite blog.db")
    ap.add_argument("--offline", action="store_true", help="no API calls, no spend")
    args = ap.parse_args()

    lengths = [int(x) for x in args.lengths.split(",")]
    depths = [float(x) for x in args.depths.split(",")]

    corpus = _load_corpus(args)
    counter: TokenCounter
    if args.offline:
        counter, model = RoughTokenCounter(), OfflineModel()
        print("OFFLINE: harness only. Every result below is 0% by construction.", file=sys.stderr)
    else:
        import anthropic  # imported late so --offline needs no SDK and no key

        client = anthropic.Anthropic()
        counter, model = ApiTokenCounter(client, args.model), ClaudeModel(client, args.model)

    probe = RecallProbe(model, Haystack(corpus, counter), trials=args.trials)
    trials = probe.sweep(lengths, depths, STRICT_NEEDLE, args.distractors)
    if args.distractors:
        print(f"planted {args.distractors} near-miss distractors", file=sys.stderr)
    print(Report(trials).table())


def _load_corpus(args: argparse.Namespace) -> Corpus:
    if args.blog_db:
        return Corpus.from_blog_db(args.blog_db)
    if args.corpus:
        return Corpus.from_dir(args.corpus)
    filler = Path(__file__).parent / "filler.md"
    if not filler.exists():
        sys.exit("no corpus: pass --corpus DIR or --blog-db PATH, or add filler.md")
    return Corpus.from_dir(filler.parent)


if __name__ == "__main__":
    main()
