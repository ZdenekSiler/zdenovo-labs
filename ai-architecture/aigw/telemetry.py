"""OpenTelemetry for the gateway, following the GenAI semantic conventions.

Names checked 2026-10-18 against open-telemetry/semantic-conventions-genai (status:
Development). That repo replaced the gen-ai pages in the main semconv repo, and inference
metrics were renamed on the way (gen_ai.client.inference.*). Pin your instrumentation version.
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from dataclasses import dataclass

from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind, Status, StatusCode

from .catalog import Deployment
from .types import ChatRequest, ChatResponse, GatewayError, Principal

PROVIDER_NAME = {"anthropic": "anthropic"}  # well-known values; others use your own name
DURATION_BUCKETS = [0.01, 0.02, 0.04, 0.08, 0.16, 0.32, 0.64, 1.28, 2.56, 5.12, 10.24,
                    20.48, 40.96, 81.92]  # the spec's recommended boundaries, in seconds


@dataclass
class TelemetryConfig:
    capture_content: bool = False  # opt-in, per the spec. And only ever AFTER redaction.
    service_name: str = "aigw"


class Telemetry:
    def __init__(self, config: TelemetryConfig | None = None,
                 tracer_provider: TracerProvider | None = None,
                 meter_provider: MeterProvider | None = None):
        self.config = config or TelemetryConfig()
        # In the lab: in-memory exporters we can print. In production: pass providers wired
        # to an OTLP exporter with a BatchSpanProcessor (Simple* exports synchronously).
        if tracer_provider is None:
            self.spans = InMemorySpanExporter()
            tracer_provider = TracerProvider()
            tracer_provider.add_span_processor(SimpleSpanProcessor(self.spans))
        if meter_provider is None:
            self.metrics = InMemoryMetricReader()
            meter_provider = MeterProvider(metric_readers=[self.metrics], views=[View(
                instrument_name="gen_ai.client.inference.*duration",
                aggregation=ExplicitBucketHistogramAggregation(DURATION_BUCKETS))])
        self.tracer_provider = tracer_provider
        self.tracer = tracer_provider.get_tracer("aigw")
        m = meter_provider.get_meter("aigw")
        self.duration = m.create_histogram("gen_ai.client.inference.duration", unit="s")
        self.ttfc = m.create_histogram("gen_ai.client.inference.time_to_first_chunk", unit="s")
        self.tok = {kind: m.create_counter(f"gen_ai.client.inference.usage.{kind}", unit="{token}")
                    for kind in ("input_tokens", "output_tokens", "cache_read.input_tokens",
                                 "cache_write.input_tokens")}
        # Not in the spec: the gateway's own business metrics, under our own namespace.
        self.cost = m.create_counter("aigw.cost", unit="USD")
        self.verdicts = m.create_counter("aigw.guardrail.verdicts")
        self.cache = m.create_counter("aigw.cache.lookups")
        self.requests = m.create_counter("aigw.requests")

    # --- spans --------------------------------------------------------------------------
    @contextmanager
    def request(self, alias: str, stream: bool):
        with self.tracer.start_as_current_span(f"gateway chat {alias}", kind=SpanKind.SERVER) as s:
            s.set_attribute("aigw.alias", alias)
            s.set_attribute("aigw.stream", stream)
            yield s

    def annotate_principal(self, span, p: Principal, req: ChatRequest) -> None:
        span.set_attribute("aigw.tenant", p.tenant)
        span.set_attribute("aigw.key_id", p.key_id)  # the prefix, never the key
        if feature := req.metadata.get("feature"):
            span.set_attribute("aigw.feature", feature)
        if pv := req.metadata.get("prompt_version"):
            span.set_attribute("aigw.prompt_version", pv)

    @contextmanager
    def guard(self, stage: str):
        with self.tracer.start_as_current_span(f"guardrails {stage}", kind=SpanKind.INTERNAL) as s:
            yield s

    def record_guard(self, span, stage: str, result) -> None:
        span.set_attribute("aigw.guardrail.verdict", result.verdict.name)
        if result.reasons:
            span.set_attribute("aigw.guardrail.reasons", result.reasons)
        for r in result.results:
            self.verdicts.add(1, {"aigw.guardrail.rail": r.rail, "aigw.guardrail.stage": stage,
                                  "aigw.guardrail.verdict": r.verdict.name})

    @contextmanager
    def inference(self, d: Deployment, req: ChatRequest, attempt: int):
        attrs = {"gen_ai.operation.name": "chat",
                 "gen_ai.provider.name": PROVIDER_NAME.get(d.provider, d.provider),
                 "gen_ai.request.model": d.model}
        t0 = time.perf_counter()
        # start_span, not start_as_current_span: this block may live inside an async
        # generator (streaming), and attaching context across yields breaks detach.
        s = self.tracer.start_span(f"chat {d.model}", kind=SpanKind.CLIENT, attributes=attrs)
        try:
            s.set_attribute("gen_ai.request.max_tokens", req.max_tokens)
            if req.stream:
                s.set_attribute("gen_ai.request.stream", True)
            s.set_attribute("aigw.attempt", attempt)
            if self.config.capture_content:
                s.set_attribute("gen_ai.input.messages", json.dumps(
                    [{"role": m.role, "parts": [{"type": "text", "content": m.content}]}
                     for m in req.messages]))
            rec = _Recorder(self, s, attrs, t0)
            try:
                yield rec
            except GatewayError as e:
                s.set_attribute("error.type", e.kind.value)
                s.set_status(Status(StatusCode.ERROR, e.kind.value))
                self.duration.record(time.perf_counter() - t0, {**attrs, "error.type": e.kind.value})
                raise
        finally:
            s.end()

    def record_cost(self, p: Principal, req: ChatRequest, usd: float) -> None:
        # Tenant on a metric is fine for tens of tenants, not for 100k self-serve accounts:
        # then put it on spans/logs only and aggregate cost offline.
        self.cost.add(usd, {"aigw.tenant": p.tenant,
                            "aigw.feature": req.metadata.get("feature", "unknown")})

    # --- inspection helpers for the lab ------------------------------------------------------
    def finished(self) -> list[ReadableSpan]:
        return list(self.spans.get_finished_spans())

    def metric_points(self) -> dict[str, list[tuple[dict, float]]]:
        out: dict[str, list[tuple[dict, float]]] = {}
        data = self.metrics.get_metrics_data()
        for rm in data.resource_metrics:
            for sm in rm.scope_metrics:
                for metric in sm.metrics:
                    for pt in metric.data.data_points:
                        value = getattr(pt, "value", None)
                        if value is None:
                            value = pt.count  # histograms: show the count
                        out.setdefault(metric.name, []).append((dict(pt.attributes), value))
        return out


class _Recorder:
    def __init__(self, tel: Telemetry, span, attrs: dict, t0: float):
        self.tel, self.span, self.attrs, self.t0 = tel, span, attrs, t0

    def first_chunk(self) -> None:
        dt = time.perf_counter() - self.t0
        self.span.set_attribute("gen_ai.response.time_to_first_chunk", dt)
        self.tel.ttfc.record(dt, self.attrs)

    def success(self, resp: ChatResponse | None, usage=None, finish: str | None = None) -> None:
        s, u = self.span, (resp.usage if resp else usage)
        attrs = {**self.attrs, "gen_ai.response.model": resp.model if resp else self.attrs["gen_ai.request.model"]}
        if resp:
            s.set_attribute("gen_ai.response.id", resp.id)
            s.set_attribute("gen_ai.response.model", resp.model)
        s.set_attribute("gen_ai.response.finish_reasons", [resp.finish_reason if resp else finish or "stop"])
        if u is not None:
            # OTel semantics: input_tokens INCLUDES cache reads/writes (our Usage already does).
            s.set_attribute("gen_ai.usage.input_tokens", u.input_tokens)
            s.set_attribute("gen_ai.usage.output_tokens", u.output_tokens)
            if u.cache_read_tokens:
                s.set_attribute("gen_ai.usage.cache_read.input_tokens", u.cache_read_tokens)
            if u.cache_write_tokens:
                s.set_attribute("gen_ai.usage.cache_write.input_tokens", u.cache_write_tokens)
            tok_attrs = {**attrs, "gen_ai.token.modality": "text"}
            self.tel.tok["input_tokens"].add(u.input_tokens, tok_attrs)
            self.tel.tok["output_tokens"].add(u.output_tokens, tok_attrs)
            self.tel.tok["cache_read.input_tokens"].add(u.cache_read_tokens, tok_attrs)
            self.tel.tok["cache_write.input_tokens"].add(u.cache_write_tokens, tok_attrs)
        if self.tel.config.capture_content and resp:
            s.set_attribute("gen_ai.output.messages", json.dumps(
                [{"role": "assistant", "parts": [{"type": "text", "content": resp.text}],
                  "finish_reason": resp.finish_reason}]))
        self.tel.duration.record(time.perf_counter() - self.t0, attrs)


def span_tree(spans: list[ReadableSpan], keys: tuple[str, ...] = ()) -> str:
    """Render finished spans as an indented tree (children ordered by start time)."""
    by_parent: dict[int | None, list[ReadableSpan]] = {}
    ids = {s.context.span_id for s in spans}
    for s in spans:
        parent = s.parent.span_id if s.parent and s.parent.span_id in ids else None
        by_parent.setdefault(parent, []).append(s)
    lines: list[str] = []

    def walk(parent: int | None, depth: int) -> None:
        for s in sorted(by_parent.get(parent, []), key=lambda x: x.start_time):
            status = " ERROR" if s.status.status_code == StatusCode.ERROR else ""
            lines.append(f"{'  ' * depth}{s.name} [{s.kind.name}]{status}")
            for k in keys:
                if k in s.attributes:
                    v = s.attributes[k]
                    v = list(v) if isinstance(v, tuple) else v
                    lines.append(f"{'  ' * depth}  · {k} = {v}")
            walk(s.context.span_id, depth + 1)

    walk(None, 0)
    return "\n".join(lines)


@dataclass
class TailSampler:
    """Decide AFTER a trace completes. Head sampling at 1% throws away 99% of your incidents.

    In production this runs in the OpenTelemetry Collector (tail_sampling processor), which
    buffers spans per trace; this class is the same policy, written out so you can test it.
    """

    slow_seconds: float = 8.0
    expensive_usd: float = 0.05
    baseline_rate: float = 0.01

    def decide(self, spans: list[ReadableSpan]) -> tuple[bool, str]:
        root = next(s for s in spans if s.parent is None)
        if any(s.status.status_code == StatusCode.ERROR for s in spans) or "error.type" in root.attributes:
            return True, "error"
        if any(s.attributes.get("aigw.guardrail.verdict", "ALLOW") != "ALLOW" for s in spans):
            return True, "guardrail"
        if (root.end_time - root.start_time) / 1e9 > self.slow_seconds:
            return True, "slow"
        if root.attributes.get("aigw.cost_usd", 0) > self.expensive_usd:
            return True, "expensive"
        # Deterministic on trace ID, so every collector replica makes the same call.
        if (root.context.trace_id % 10_000) < self.baseline_rate * 10_000:
            return True, "baseline"
        return False, "dropped"
