"""Model catalog: aliases callers use, deployments the gateway routes to, and prices as data."""

from __future__ import annotations

from dataclasses import dataclass, field

from .types import ErrorKind, GatewayError, Usage


@dataclass(frozen=True, slots=True)
class Price:
    """USD per million tokens. Verify against the provider's pricing page on publish day."""

    input: float
    output: float
    cache_read: float | None = None  # None = unknown, bill at the full input price
    cache_write: float | None = None

    def cost(self, u: Usage) -> float:
        cache_read = self.cache_read if self.cache_read is not None else self.input
        cache_write = self.cache_write if self.cache_write is not None else self.input
        return (u.uncached_input_tokens * self.input
                + u.cache_read_tokens * cache_read
                + u.cache_write_tokens * cache_write
                + u.output_tokens * self.output) / 1_000_000


@dataclass(frozen=True, slots=True)
class Deployment:
    provider: str  # key into the provider registry
    model: str  # the provider's model ID
    price: Price
    context_window: int
    capabilities: frozenset[str] = frozenset({"chat", "stream"})
    region: str = "global"


@dataclass(slots=True)
class Route:
    alias: str
    chain: list[Deployment]  # primary first, then fallbacks in order
    max_tokens_cap: int = 8192
    allowed_tenants: frozenset[str] | None = None  # None = everyone


@dataclass(slots=True)
class ModelCatalog:
    routes: dict[str, Route] = field(default_factory=dict)

    def add(self, route: Route) -> None:
        self.routes[route.alias] = route

    def resolve(self, alias: str, tenant: str, needs: set[str] = frozenset()) -> Route:
        route = self.routes.get(alias)
        if route is None:
            # Unknown alias, or a raw vendor ID someone hard-coded. Both are config bugs.
            raise GatewayError(ErrorKind.NOT_FOUND, f"unknown model alias {alias!r}")
        if route.allowed_tenants is not None and tenant not in route.allowed_tenants:
            raise GatewayError(ErrorKind.NOT_FOUND, f"unknown model alias {alias!r}")
        chain = [d for d in route.chain if needs <= d.capabilities]
        if not chain:
            raise GatewayError(ErrorKind.INVALID_REQUEST,
                               f"no deployment of {alias!r} supports {sorted(needs)}")
        return Route(route.alias, chain, route.max_tokens_cap, route.allowed_tenants)


# Prices: Anthropic first-party rates as listed 2026-10-06. Haiku 5.5's cache-read rate
# wasn't in the table I had, so it bills at the full input price (an overestimate).
SONNET = Price(input=2.00, output=10.00, cache_read=0.20, cache_write=2.50)
OPUS = Price(input=4.00, output=20.00, cache_read=0.20, cache_write=5.00)
HAIKU = Price(input=0.10, output=0.50)
# A self-hosted model has no per-token bill, but it isn't free: amortised GPU cost.
LOCAL = Price(input=0.05, output=0.15)


def default_catalog() -> ModelCatalog:
    cat = ModelCatalog()
    sonnet = Deployment("anthropic", "claude-sonnet-5-5", SONNET, 1_000_000,
                        frozenset({"chat", "stream", "tools", "vision"}))
    opus = Deployment("anthropic", "claude-opus-5-5", OPUS, 1_000_000,
                      frozenset({"chat", "stream", "tools", "vision"}))
    haiku = Deployment("anthropic", "claude-haiku-5-5", HAIKU, 1_000_000,
                       frozenset({"chat", "stream", "tools"}))
    local = Deployment("selfhosted", "llama-local-70b", LOCAL, 128_000,
                       frozenset({"chat", "stream"}), region="eu-onprem")
    cat.add(Route("chat-default", [sonnet, local]))
    cat.add(Route("chat-premium", [opus, sonnet], allowed_tenants=frozenset({"acme"})))
    cat.add(Route("chat-cheap", [haiku, local]))
    cat.add(Route("chat-eu-only", [local]))  # data residency: never leaves the region
    return cat
