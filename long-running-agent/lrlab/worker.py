"""The worker: claim a conversation, run the harness on it, release it with a status.

Run as many workers as you like (processes, hosts). The lease makes sure each conversation is
only run by one of them at a time.
"""
from __future__ import annotations

from .config import LabConfig
from .conv_queue import ConversationQueue
from .harness import AgentHarness, Outcome
from .models.base import ModelClient
from .runner import CommandRunner
from .state import StateStore
from .task import SYSTEM_PROMPT, done_check, quiet_check
from .tools import ToolRegistry, default_tools

CONV_STATUS = {
    "done": "done", "quiet": "done", "parked_human": "parked_human", "parked_retry": "parked_retry",
    "parked_budget": "parked_budget", "failed": "failed",
}


class Worker:
    def __init__(self, cfg: LabConfig, model: ModelClient, *, owner: str | None = None,
                 tools: ToolRegistry | None = None, slow_tools_s: float = 0.0, sleep=None,
                 system_prompt: str = SYSTEM_PROMPT):
        cfg.workspace.mkdir(parents=True, exist_ok=True)
        self.cfg = cfg
        self.store = StateStore(cfg.db_path)
        self.queue = ConversationQueue(self.store, cfg.lease_s, owner)
        self.runner = CommandRunner(cfg.workspace, cfg.pypi_base, slow_s=slow_tools_s)
        kwargs = {"sleep": sleep} if sleep else {}
        self.harness = AgentHarness(
            store=self.store, queue=self.queue, runner=self.runner, model=model,
            tools=tools or default_tools(), budget=cfg.budget, system_prompt=system_prompt,
            done_check=done_check, quiet_check=quiet_check, retry_base_s=cfg.model_retry_base_s, **kwargs)

    def run_once(self) -> tuple[str, Outcome] | None:
        lease = self.queue.claim()
        if lease is None:
            return None
        outcome = self.harness.run(lease)
        if outcome.status != "lease_lost":          # a fenced-out worker must not touch the row
            self.queue.release(lease, CONV_STATUS[outcome.status], outcome.retry_at)
        return lease.conv_id, outcome

    def run_until_idle(self, max_runs: int = 50) -> list[tuple[str, Outcome]]:
        done = []
        for _ in range(max_runs):
            r = self.run_once()
            if r is None:
                break
            done.append(r)
        return done

    def close(self) -> None:
        self.runner.close()
