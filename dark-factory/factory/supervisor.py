"""Supervisor: runs the stages in order, keeps N worker processes alive, and refuses to start a
stage until the previous one's gate passes. It holds no state of its own: kill it and re-run,
and it picks up from the queue."""
import fcntl
import multiprocessing as mp
import os
import time
from pathlib import Path

from .errors import Fatal, Offline, Retryable
from .stages import PIPELINE, STAGES
from .workspace import Workspace


def worker_main(root: str, stage_name: str, worker_id: str):
    ws = Workspace.open(Path(root), pin=False)
    stage = STAGES[stage_name]
    q = ws.queue
    while not ws.stop_requested():
        task = q.claim(stage.name, worker_id, stage.lease_s)
        if task is None:
            if q.drained(stage.name):
                return
            time.sleep(0.5)  # everything left is running elsewhere or backing off
            continue
        try:
            result = stage.run(task, ws)
            problems = stage.verify(task, result, ws)
            if problems:
                q.fail(task, "verify: " + "; ".join(problems), retryable=True)
            else:
                q.complete(task, result)
        except Fatal as e:
            q.fail(task, f"fatal: {e}", retryable=False)
        except Offline as e:  # before Retryable: it's a subclass, but must not cost an attempt
            q.offline(task, e.dependency, str(e))
        except Retryable as e:
            q.fail(task, str(e), retryable=True)
        except Exception as e:  # unknown failure: retry, but the attempt cap still applies
            q.fail(task, f"{type(e).__name__}: {e}", retryable=True)


class Supervisor:
    def __init__(self, ws: Workspace, log=print):
        self.ws = ws
        self.log = log
        self.started = time.time()
        self.generation = 0
        self.run_id = os.getpid()

    def run(self) -> bool:
        # One supervisor per workdir. flock dies with the process, even on kill -9, so there's
        # no stale lock to clean up. (Found the hard way: my "kill -9" missed, a second
        # supervisor started, and both ran on one queue for four minutes.)
        self._lock = open(self.ws.root / "supervisor.lock", "w")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("another supervisor is already running on this workdir; refusing to start")
        q = self.ws.queue
        q.event("run_start", f"workers={self.ws.cfg.workers} chaos={self.ws.cfg.chaos}")
        if self.ws.stop_requested():
            self.ws.stop_file.unlink()  # a new run clears yesterday's kill switch
        skipped = []
        for stage in PIPELINE:
            if stage.uses_llm and not self.ws.llm.ping():
                self.log(f"[{stage.name}] Ollama/model {self.ws.cfg.model!r} not reachable; tasks will retry with backoff")
            new = q.enqueue(stage.name, stage.plan(self.ws), stage.max_attempts)
            self.log(f"[{stage.name}] planned {new} new task(s), {sum(q.counts(stage.name).values())} total")
            self._drain(stage)
            if self.ws.stop_requested():
                self.log(f"[{stage.name}] STOP requested: halting cleanly, re-run to resume")
                q.event("halt", f"stop requested during {stage.name}")
                return False
            problems = stage.gate(self.ws)
            q.record_gate(stage.name, problems)
            if problems and not stage.blocking:
                # Enrichment failed: say so loudly, but don't hold the main result hostage.
                self.log(f"[{stage.name}] GATE FAILED (non-blocking): {problems}. Continuing without it.")
                q.event("warning", f"{stage.name} gate failed, results reported without it: {problems}")
                skipped.append(stage.name)
                continue
            if problems:
                self.log(f"[{stage.name}] GATE FAILED: {problems}. Not starting the next stage.")
                q.event("halt", f"gate failed at {stage.name}")
                return False
            self.log(f"[{stage.name}] gate passed")
        q.event("run_end", f"blocking gates passed; reported without: {skipped}" if skipped else "all gates passed")
        return not skipped

    def _spawn(self, stage_name: str, slot: int) -> tuple[str, mp.Process]:
        self.generation += 1
        # Unique per restart AND per run: fencing compares worker ids, so two runs both
        # naming a worker "w0.9" would let a stale worker complete the other's task.
        wid = f"w{slot}.{self.generation}@{self.run_id}"
        p = mp.Process(target=worker_main, args=(str(self.ws.root), stage_name, wid), daemon=True)
        p.start()
        return wid, p

    def _drain(self, stage):
        q, cfg = self.ws.queue, self.ws.cfg
        if stage.uses_llm:
            n = min(cfg.workers, cfg.llm_workers)
        elif stage.uses_network:
            n = min(cfg.workers, cfg.net_workers)
        else:
            n = cfg.workers
        slots = {i: self._spawn(stage.name, i) for i in range(n)}
        last_print = 0.0
        last_tick = time.time()
        q.beat(last_tick)
        while True:
            now = time.time()
            if now - last_tick > 60:
                # The loop sleeps 0.5 s, so a minute between ticks means this whole process
                # tree was frozen: laptop sleep, a suspended VM, SIGSTOP. Every worker was
                # frozen too, so nobody's lease should have run out.
                gap = now - last_tick
                extended = q.extend_leases(gap)
                q.event("host_suspended", f"{gap:.0f}s without a tick; extended {extended} running lease(s)")
                self.log(f"[{stage.name}] host was frozen for {gap / 60:.0f} min; extended {extended} lease(s)")
            last_tick = now
            q.beat(now)
            for i, (wid, p) in list(slots.items()):
                if p.is_alive() or p.exitcode is None:
                    continue
                if p.exitcode != 0:
                    q.event("worker_died", f"exit code {p.exitcode}", worker=wid)
                    self.log(f"[{stage.name}] worker {wid} died (exit {p.exitcode}); restarting")
                    slots[i] = self._spawn(stage.name, i)
                else:
                    slots.pop(i)
            if not slots:
                break
            if cfg.deadline_min and time.time() - self.started > cfg.deadline_min * 60:
                self.ws.stop_file.touch()  # workers finish their current task, then exit
            if time.time() - last_print > 10:
                c = q.counts(stage.name)
                line = " ".join(f"{k}={v}" for k, v in sorted(c.items()))
                circuit = q.circuit(stage.name)
                if circuit:
                    line += f"  PAUSED: {circuit[0]} unreachable for {time.time() - circuit[1]:.0f}s"
                self.log(f"[{stage.name}] {line}")
                last_print = time.time()
            time.sleep(0.5)
