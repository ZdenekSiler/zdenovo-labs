"""Config, on-disk layout and the chaos monkey, shared by the supervisor and every worker."""
import json
import os
import random
import time
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from pathlib import Path

from .errors import Offline, Retryable
from .fetch import PoliteFetcher
from .llm import OllamaClient
from .queue import Task, TaskQueue


@dataclass
class Config:
    offers: str = "prodej,pronajem"  # sale and rent: both are needed for the rental yield
    max_per_offer: int = 0           # 0 = every flat listing in the sitemap (~8k pages, ~2.5 h at 1 req/s)
    ruian_date: str = ""             # RÚIAN monthly snapshot (YYYYMMDD); pinned on first run
    # Measured on the canaries: llama3.2 3B got 14/18 right at ~10 s a call, llama3 8B 17/18 at
    # ~21 s. With two canaries per batch the 3B passes ~60% of batches, the 8B ~89%, so the
    # "cheap" model costs more once retries and dead letters are counted.
    model: str = "llama3"
    escalate_model: str = ""       # optional cascade target; needs RAM for BOTH models (OOM'd Ollama on 14 GB)
    ollama: str = "http://localhost:11434"
    workers: int = 8
    llm_workers: int = 2           # Ollama on a laptop CPU doesn't get faster with 8 callers
    net_workers: int = 2           # the rate limit is global anyway; 2 keeps one waiting, not 8
    net_delay: float = 1.0         # seconds between requests to the same host, across all workers
    listing_batch: int = 25
    signal_batch: int = 6          # ads are long; 6 + 2 canaries fits a 3B model's attention
    shard_files: int = 250         # RÚIAN municipal CSVs per parallel task (6,258 files total)
    min_listings: int = 10         # a city needs this many sale AND rent listings to be reported
    error_budget_pct: float = 2.0  # max share of sale listings allowed to end up without signals
    chaos: float = 0.0             # probability per fault point; 0 in production, 0.1 in the demo
    chaos_faults: str = ""         # comma-separated subset of FAULTS; "" = all of them
    deadline_min: float = 0.0      # 0 = no deadline


# Every fault mimics something that actually went wrong while building this lab.
FAULTS = {
    "net_drop":    "connection reset mid-transfer (ČÚZK dropped the 63 MB file at 56 MB)",
    "crash":       "worker killed mid-task with exit 137, like the OOM killer",
    "hang":        "worker stalls past its lease, then finishes anyway",
    "corrupt":     "a RÚIAN shard silently loses one municipality's addresses",
    "poison":      "a U+2028 LINE SEPARATOR in an ad (it once dead-lettered a batch)",
    "pii_leak":    "a phone number slips past redaction into a stored ad",
    "llm_garbage": "the model answers with a label that doesn't exist",
    "llm_down":    "Ollama unreachable (it was OOM-killed twice)",
}
# Out-of-process drills (scripts/chaos.sh): freeze (laptop sleep), kill (kill -9), blackout.


class Chaos:
    """Deterministic fault injection: seeded by task id + attempt + fault, so a chaos run is
    reproducible and a retry gets a fresh, independent roll. Every injection is logged, so the
    morning report can put "injected" next to "recovered"."""

    def __init__(self, rate: float, faults: str, log):
        self.rate = rate
        self.enabled = {f for f in faults.split(",") if f} or set(FAULTS)
        unknown = self.enabled - set(FAULTS)
        if unknown:
            raise SystemExit(f"unknown chaos fault(s) {sorted(unknown)}; known: {sorted(FAULTS)}")
        self.log = log

    def fires(self, task: Task, fault: str, salt: object = "") -> bool:
        if fault not in self.enabled or self.rate <= 0:
            return False
        if random.Random(f"{task.id}:{task.attempts}:{fault}:{salt}").random() >= self.rate:
            return False
        self.log("chaos", f"{fault}: {FAULTS[fault]}", task.id, task.worker)
        return True

    # the faults whose effect is the same wherever they fire
    def net_drop(self, task: Task):
        if self.fires(task, "net_drop"):
            raise Retryable("[chaos] connection reset by peer")

    def crash(self, task: Task):
        if self.fires(task, "crash"):
            os._exit(137)  # no cleanup, no finally, no goodbye

    def hang(self, task: Task, seconds: float):
        if self.fires(task, "hang"):
            time.sleep(seconds)

    def llm_down(self, task: Task):
        # Offline refunds the attempt, so a roll seeded only on the attempt number would fire
        # again on every retry, forever. The clock moves on between probes; the attempt doesn't.
        if self.fires(task, "llm_down", int(time.time()) // 15):
            raise Offline("ollama", "[chaos] connection refused")


class Workspace:
    def __init__(self, root: Path, cfg: Config):
        self.root = root
        self.cfg = cfg
        self.raw = root / "raw"
        self.listings = root / "listings"
        self.ruian = root / "ruian"
        self.census = root / "census"
        self.signals = root / "signals"
        self.out = root / "out"
        for d in (self.raw, self.listings, self.ruian, self.census, self.signals, self.out):
            d.mkdir(parents=True, exist_ok=True)
        self.queue = TaskQueue(str(root / "factory.db"))
        self.llm = OllamaClient(cfg.model, cfg.ollama)
        self.fetcher = PoliteFetcher(root / ".rate", delay=cfg.net_delay)
        self.chaos = Chaos(cfg.chaos, cfg.chaos_faults, self.queue.event)

    @classmethod
    def open(cls, root: Path, overrides: dict | None = None, pin: bool = True) -> "Workspace":
        """The first run pins the config to disk. A resume reuses it, so restarting with a
        different --offers can't silently re-plan half the work. Only runtime knobs
        (workers, chaos, deadline) may change between runs."""
        root.mkdir(parents=True, exist_ok=True)
        path = root / "config.json"
        cfg = Config(**json.loads(path.read_text())) if path.exists() else Config()
        runtime = {"workers", "llm_workers", "net_workers", "chaos", "chaos_faults", "deadline_min",
                   "ollama", "escalate_model"}
        for k, v in (overrides or {}).items():
            if v is None:
                continue
            if path.exists() and k not in runtime and getattr(cfg, k) != v:
                raise SystemExit(f"{k} is pinned to {getattr(cfg, k)!r} in {path}; reset to change it")
            setattr(cfg, k, v)
        if not cfg.ruian_date:  # RÚIAN publishes on the last day of each month
            cfg.ruian_date = (date.today().replace(day=1) - timedelta(days=1)).strftime("%Y%m%d")
        if pin:  # only the parent writes; workers reading a half-written file crashed on day one
            atomic_write(path, json.dumps(asdict(cfg), indent=2))
        return cls(root, cfg)

    @property
    def stop_file(self) -> Path:
        return self.root / "STOP"

    def stop_requested(self) -> bool:
        return self.stop_file.exists()

    def read_jsonl(self, directory: Path):
        for f in sorted(directory.glob("*.jsonl")):
            yield from read_jsonl_file(f)


def read_jsonl_file(path: Path) -> list[dict]:
    """Split on "\n" only. str.splitlines() also breaks on U+2028, which json.dumps(...,
    ensure_ascii=False) leaves raw inside strings. One seller pasted it into an ad, the line
    came apart, and that batch failed identically four times: a poison pill made of whitespace."""
    return [json.loads(line) for line in path.read_text().split("\n") if line]


def atomic_write(path: Path, text: str):
    """Write to a temp file, then rename. Readers see the old file or the whole new one,
    never half of it, and a crash mid-write leaves only a stray .tmp behind."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)
