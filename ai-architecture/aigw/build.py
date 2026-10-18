"""Composition root for the lab: wires a Gateway to scripted fake upstreams.

In production this is the only file that changes: real clients, a Redis-backed limiter,
OTLP exporters, providers from config.
"""

from __future__ import annotations

from dataclasses import dataclass

from .cache import ExactCache
from .catalog import ModelCatalog, default_catalog
from .fakes import FakeUpstream, anthropic_client, selfhosted_client
from .gateway import Gateway, GatewayConfig
from .guardrails import GuardrailPipeline, InjectionHeuristic, PiiRedactor, SecretScanner
from .limits import BudgetLedger, TokenRateLimiter
from .providers.anthropic_provider import AnthropicProvider
from .providers.selfhosted import SelfHostedProvider
from .resilience import CircuitBreaker, FakeClock, ResilientRouter, RetryBudget, RetryPolicy
from .security import KeyStore
from .telemetry import Telemetry, TelemetryConfig


@dataclass
class Lab:
    gateway: Gateway
    anthropic: FakeUpstream
    selfhosted: FakeUpstream
    clock: FakeClock
    keys: KeyStore
    acme_key: str
    globex_key: str


def build_lab(*, anthropic_script=None, selfhosted_script=None, anthropic_responder=None,
              selfhosted_responder=None, catalog: ModelCatalog | None = None,
              rpm: int = 600, tpm: int = 2_000_000, capture_content: bool = False,
              input_rails=None, output_rails=None, sdk_retries: int = 0,
              retry_budget: RetryBudget | None = None) -> Lab:
    clock = FakeClock()
    fa = FakeUpstream("anthropic", list(anthropic_script or []))
    fs = FakeUpstream("selfhosted", list(selfhosted_script or []))
    if anthropic_responder:
        fa.responder = anthropic_responder
    if selfhosted_responder:
        fs.responder = selfhosted_responder
    providers = {
        "anthropic": AnthropicProvider(anthropic_client(fa), sdk_retries=sdk_retries),
        "selfhosted": SelfHostedProvider("http://gpu-pool.internal:8000", selfhosted_client(fs)),
    }
    router = ResilientRouter(providers, clock=clock, policy=RetryPolicy(),
                             budget=retry_budget or RetryBudget(),
                             breaker_factory=lambda: CircuitBreaker(clock, window=10, min_calls=4,
                                                                    failure_rate=0.5, cooldown=30))
    keys = KeyStore(pepper=b"lab-pepper-from-secret-manager")
    acme_key, _ = keys.issue("acme", "alice", {"chat"})
    globex_key, _ = keys.issue("globex", "bob", {"chat"})
    budgets = BudgetLedger()
    budgets.set_limit("tenant:acme", 50.0)
    budgets.set_limit("tenant:globex", 0.002)  # a tiny budget, to watch it run out
    gw = Gateway(
        keys=keys, catalog=catalog or default_catalog(), router=router,
        limiter=TokenRateLimiter(clock, rpm=rpm, tpm=tpm), budgets=budgets,
        input_rails=input_rails or GuardrailPipeline([PiiRedactor(), SecretScanner(), InjectionHeuristic()]),
        output_rails=output_rails or GuardrailPipeline([PiiRedactor(), SecretScanner()]),
        telemetry=Telemetry(TelemetryConfig(capture_content=capture_content)),
        cache=ExactCache(clock), clock=clock, config=GatewayConfig())
    return Lab(gw, fa, fs, clock, keys, acme_key, globex_key)
