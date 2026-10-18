"""Config checks that run in CI on every change to the catalog, limits or prompts.

Cheap, deterministic, and they catch the outage-time surprises at review time instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .catalog import ModelCatalog
from .guardrails import SECRET_PATTERNS

UNPINNED = re.compile(r"(latest|preview|-exp\b)", re.I)


@dataclass
class Finding:
    severity: str  # "error" fails CI, "warn" is printed
    where: str
    message: str


def check_catalog(cat: ModelCatalog, needs_by_alias: dict[str, set[str]]) -> list[Finding]:
    out: list[Finding] = []
    for alias, route in cat.routes.items():
        for d in route.chain:
            if UNPINNED.search(d.model):
                out.append(Finding("error", alias, f"{d.model} is not a pinned version"))
        for cap in sorted(needs_by_alias.get(alias, set()) | {"chat"}):
            capable = [d for d in route.chain if cap in d.capabilities]
            if not capable:
                out.append(Finding("error", alias, f"no deployment supports {cap!r}"))
            elif len(capable) == 1:
                out.append(Finding("warn", alias, f"no fallback for {cap!r} (only {capable[0].model})"))
        regions = {d.region for d in route.chain}
        if alias.endswith("-eu-only") and regions - {"eu-onprem", "eu"}:
            out.append(Finding("error", alias, f"residency alias reaches {sorted(regions)}"))
    return out


def check_tenant_limits(tenant_tpm: dict[str, int], provider_tpm: int) -> list[Finding]:
    total = sum(tenant_tpm.values())
    if total > provider_tpm:
        return [Finding("error", "limits", f"tenant TPM sums to {total:,} > provider limit {provider_tpm:,}")]
    return []


def check_prompts(prompts: dict[str, str]) -> list[Finding]:
    out = []
    for name, text in prompts.items():
        hits = [n for n, p in SECRET_PATTERNS if p.search(text)]
        if hits:
            out.append(Finding("error", name, f"secret in prompt: {hits}"))
        if re.search(r"\{\{?\s*(now|today|date|time)", text) or re.search(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:", text):
            out.append(Finding("warn", name, "time-like value in prompt: breaks prompt caching"))
    return out
