"""Guardrail pipeline: tiered, concurrent within a tier, and only ever stricter."""

from __future__ import annotations

import asyncio
import enum
import json
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol

from pydantic import BaseModel, ValidationError

from .types import Principal, StreamChunk

Stage = Literal["input", "output"]


class Verdict(enum.IntEnum):
    """Ordered. combine() is max(): no rail can make another rail's decision more lenient."""

    ALLOW = 0
    FLAG = 1  # allow, but record it (telemetry, sampling for review)
    REDACT = 2  # allow a modified text
    REVIEW = 3  # hold for a human / async review queue
    BLOCK = 4


@dataclass
class GuardResult:
    rail: str
    verdict: Verdict
    reason: str = ""
    text: str | None = None  # new text, when verdict == REDACT
    ms: float = 0.0


@dataclass
class PiiVault:
    """Per-request placeholder <-> value map, so redaction can be reversed for the user."""

    values: dict[str, str] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)

    def placeholder(self, kind: str, value: str) -> str:
        for k, v in self.values.items():
            if v == value:
                return k
        self.counters[kind] = self.counters.get(kind, 0) + 1
        key = f"<{kind}_{self.counters[kind]}>"
        self.values[key] = value
        return key

    def restore(self, text: str, only: set[str] | None = None) -> str:
        for k, v in self.values.items():
            if only is None or k in only:
                text = text.replace(k, v)
        return text


@dataclass
class GuardContext:
    stage: Stage
    text: str
    principal: Principal | None = None
    vault: PiiVault = field(default_factory=PiiVault)
    untrusted: bool = False  # text came from a document/web page/tool, not from the user


class Guardrail(Protocol):
    name: str
    stages: set[Stage]
    tier: int  # 0 deterministic (~µs), 1 classifier (~10ms), 2 LLM judge (~100ms-1s)
    fail_closed: bool  # what an exception or timeout in THIS rail means
    timeout: float

    async def check(self, ctx: GuardContext) -> GuardResult: ...


# --- Deterministic rails ---------------------------------------------------------------


def luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total, alt = total + d, not alt
    return total % 10 == 0


def iban_ok(raw: str) -> bool:
    s = raw.replace(" ", "")
    s = s[4:] + s[:4]
    return int("".join(str(int(c, 36)) for c in s)) % 97 == 1


PII_PATTERNS: list[tuple[str, re.Pattern, Callable[[str], bool]]] = [
    ("EMAIL", re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}\b"), lambda s: True),
    ("IBAN", re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}(?: ?[A-Z0-9]{1,3})?\b"), iban_ok),
    ("CARD", re.compile(r"\b\d(?:[ -]?\d){12,18}\b"),
     lambda s: luhn_ok(re.sub(r"\D", "", s))),
    ("PHONE", re.compile(r"(?<!\w)\+\d{1,3}[ .-]?\d{2,4}(?:[ .-]?\d{2,4}){2,3}\b"), lambda s: True),
]


class PiiRedactor:
    name, stages, tier, fail_closed, timeout = "pii_redactor", {"input", "output"}, 0, True, 0.05

    async def check(self, ctx: GuardContext) -> GuardResult:
        text, found = ctx.text, []
        for kind, pattern, valid in PII_PATTERNS:
            def repl(m: re.Match, kind=kind, valid=valid) -> str:
                if not valid(m.group(0)):
                    return m.group(0)  # e.g. a 16-digit order number that fails Luhn
                found.append(kind)
                return ctx.vault.placeholder(kind, m.group(0))
            text = pattern.sub(repl, text)
        if not found:
            return GuardResult(self.name, Verdict.ALLOW)
        return GuardResult(self.name, Verdict.REDACT, f"redacted {sorted(set(found))}", text)


SECRET_PATTERNS = [
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("gateway_key", re.compile(r"aigw_live_[A-Za-z0-9_-]{20,}")),
]


class SecretScanner:
    name, stages, tier, fail_closed, timeout = "secret_scanner", {"input", "output"}, 0, True, 0.05

    async def check(self, ctx: GuardContext) -> GuardResult:
        hits = [n for n, p in SECRET_PATTERNS if p.search(ctx.text)]
        if not hits:
            return GuardResult(self.name, Verdict.ALLOW)
        # On input, someone pasted a credential: redact it, don't send it to a vendor.
        # On output, the model is emitting one: that's an incident, not a redaction.
        if ctx.stage == "input":
            text = ctx.text
            for _, p in SECRET_PATTERNS:
                text = p.sub("<SECRET>", text)
            return GuardResult(self.name, Verdict.REDACT, f"secret in input {hits}", text)
        return GuardResult(self.name, Verdict.BLOCK, f"secret in output {hits}")


INJECTION_HINTS = re.compile(
    r"ignore (all |any )?(previous|prior|above) instructions|you are now|system prompt|"
    r"disregard .{0,20}(rules|instructions)|<\s*/?\s*system\s*>", re.I)


class InjectionHeuristic:
    """Telemetry, not a wall. Adaptive attackers walk around any pattern list (part 5)."""

    name, stages, tier, fail_closed, timeout = "injection_heuristic", {"input"}, 0, False, 0.05

    async def check(self, ctx: GuardContext) -> GuardResult:
        if m := INJECTION_HINTS.search(ctx.text):
            where = "untrusted content" if ctx.untrusted else "user message"
            return GuardResult(self.name, Verdict.FLAG, f"injection phrase in {where}: {m.group(0)!r}")
        return GuardResult(self.name, Verdict.ALLOW)


# --- Model-backed rails ------------------------------------------------------------------


Classifier = Callable[[str], Awaitable[dict[str, float]]]  # text -> {label: probability}


@dataclass
class ClassifierRail:
    """Wraps any scorer (a hosted moderation API, a small local model, an LLM judge).

    `thresholds` maps label -> (review_at, block_at). Tune them on labelled traffic.
    """

    name: str
    classify: Classifier
    thresholds: dict[str, tuple[float, float]]
    stages: set[Stage] = field(default_factory=lambda: {"input", "output"})
    tier: int = 1
    fail_closed: bool = False
    timeout: float = 0.3

    async def check(self, ctx: GuardContext) -> GuardResult:
        scores = await self.classify(ctx.text)
        worst = GuardResult(self.name, Verdict.ALLOW)
        for label, (review_at, block_at) in self.thresholds.items():
            p = scores.get(label, 0.0)
            v = Verdict.BLOCK if p >= block_at else Verdict.REVIEW if p >= review_at else Verdict.ALLOW
            if v > worst.verdict:
                worst = GuardResult(self.name, v, f"{label}={p:.2f}")
        return worst


# --- The pipeline ------------------------------------------------------------------------


@dataclass
class PipelineResult:
    verdict: Verdict
    text: str
    results: list[GuardResult]

    @property
    def reasons(self) -> list[str]:
        return [f"{r.rail}:{r.verdict.name}:{r.reason}" for r in self.results if r.verdict]


class GuardrailPipeline:
    def __init__(self, rails: list[Guardrail]):
        self.rails = rails

    async def _run_one(self, rail: Guardrail, ctx: GuardContext) -> GuardResult:
        t0 = time.perf_counter()
        try:
            r = await asyncio.wait_for(rail.check(ctx), rail.timeout)
        except Exception as e:  # noqa: BLE001 - a broken rail is a policy decision, not a crash
            kind = "timeout" if isinstance(e, TimeoutError) else type(e).__name__
            v = Verdict.BLOCK if rail.fail_closed else Verdict.FLAG
            r = GuardResult(rail.name, v, f"rail failed ({kind}), fail_{'closed' if rail.fail_closed else 'open'}")
        r.ms = round((time.perf_counter() - t0) * 1000, 2)
        return r

    async def run(self, ctx: GuardContext) -> PipelineResult:
        results: list[GuardResult] = []
        verdict = Verdict.ALLOW
        rails = [r for r in self.rails if ctx.stage in r.stages]
        for tier in sorted({r.tier for r in rails}):
            in_tier = [r for r in rails if r.tier == tier]
            # Rewriting rails run in sequence so each sees the previous one's output; this is
            # also why tier-0 redaction runs BEFORE tier-1 rails that may call a third party.
            for rail in [r for r in in_tier if isinstance(r, (PiiRedactor, SecretScanner))]:
                res = await self._run_one(rail, ctx)
                if res.verdict == Verdict.REDACT and res.text is not None:
                    ctx.text = res.text
                results.append(res)
                verdict = max(verdict, res.verdict)
            rest = [r for r in in_tier if not isinstance(r, (PiiRedactor, SecretScanner))]
            for res in await asyncio.gather(*(self._run_one(r, ctx) for r in rest)):
                results.append(res)
                verdict = max(verdict, res.verdict)
            if verdict >= Verdict.BLOCK:
                break  # don't pay for the LLM judge on something a regex already blocked
        return PipelineResult(verdict, ctx.text, results)


# --- Streaming output guard --------------------------------------------------------------


async def guarded_stream(chunks: AsyncIterator[StreamChunk], pipeline: GuardrailPipeline,
                         principal: Principal | None, holdback: int = 64,
                         replacement: str = "[response withheld by policy]") -> AsyncIterator[str]:
    """Scan the whole text so far on every chunk; release all but the last `holdback` chars.

    Holdback must be >= the longest pattern you need to catch, or a secret split across two
    chunks leaks its first half before the second half arrives. Costs `holdback` chars of
    latency, and only deterministic rails are cheap enough to run per chunk.
    """
    seen, released = "", 0
    async for chunk in chunks:
        if chunk.finish_reason == "error":
            yield "\n[stream interrupted]"
            return
        seen += chunk.text
        res = await pipeline.run(GuardContext("output", seen, principal))
        if res.verdict >= Verdict.REVIEW:
            yield replacement
            return
        safe_upto = max(released, len(res.text) - holdback)
        if safe_upto > released:
            yield res.text[released:safe_upto]
            released = safe_upto
    if released < len(seen):
        res = await pipeline.run(GuardContext("output", seen, principal))
        if res.verdict >= Verdict.REVIEW:
            yield replacement
            return
        yield res.text[released:]


# --- Structured output with bounded repair -------------------------------------------------


async def parse_with_repair(model: type[BaseModel], first: str,
                            ask_again: Callable[[str], Awaitable[str]], max_repairs: int = 1):
    """Validate; on failure, send the validation error back once. Never loop unbounded.

    Prefer the provider's native structured outputs (Anthropic: output_config.format) where
    available. This is the fallback for models and gateways that don't have it.
    """
    text, errors = first, []
    for attempt in range(max_repairs + 1):
        try:
            return model.model_validate(json.loads(text)), errors
        except (json.JSONDecodeError, ValidationError) as e:
            errors.append(f"attempt {attempt + 1}: {str(e).splitlines()[0]}")
            if attempt == max_repairs:
                break
            text = await ask_again(f"Your previous output was invalid: {str(e)[:300]}. "
                                   f"Return only JSON matching the schema.")
    return None, errors
