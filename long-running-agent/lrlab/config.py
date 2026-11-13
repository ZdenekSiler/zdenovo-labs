"""Configuration objects. Everything is passed in explicitly; nothing lives at module level."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Budget:
    """Hard limits the harness enforces. The model never sees these numbers as suggestions."""

    max_turns: int = 40              # model calls per run (one run = one wake-up)
    max_tokens: int = 400_000        # input + output tokens per run
    max_wall_s: float = 1800.0       # 30 minutes of wall clock per run
    max_cost_usd: float = 1.00       # per run; free tiers and local models report 0
    context_tokens: int = 24_000     # above this, restart into a fresh context from the progress file


@dataclass(frozen=True)
class LabConfig:
    home: Path                       # one directory per deployment: db, workspace, outbox
    pypi_base: str                   # http://127.0.0.1:PORT (fixtures) or https://pypi.org (live)
    webhook_secret: str = "lab-only-secret-change-me"
    budget: Budget = field(default_factory=Budget)
    lease_s: float = 60.0            # how long a worker owns a conversation without renewing
    model_retry_base_s: float = 1.0  # backoff base for 429 / 5xx from a model provider

    @property
    def db_path(self) -> Path:
        return self.home / "state.db"

    @property
    def workspace(self) -> Path:
        return self.home / "workspace"

    @property
    def outbox(self) -> Path:
        return self.home / "outbox"
