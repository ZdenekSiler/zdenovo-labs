"""Durable task queue on SQLite: the only state that has to survive a crash.

Workers are disposable. If one dies, its lease expires and another worker picks the task up.
If the whole machine reboots, `run.py run` re-plans (idempotently) and carries on from here.
"""
import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id           TEXT PRIMARY KEY,
  stage        TEXT NOT NULL,
  payload      TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'pending',  -- pending | running | done | dead
  attempts     INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL,
  not_before   REAL NOT NULL DEFAULT 0,          -- backoff: don't claim before this time
  lease_until  REAL,                             -- running tasks past this are up for grabs
  worker       TEXT,
  result       TEXT,
  last_error   TEXT,
  updated      REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  ts REAL NOT NULL, task_id TEXT, worker TEXT, kind TEXT NOT NULL, detail TEXT
);
CREATE TABLE IF NOT EXISTS gates (
  stage TEXT PRIMARY KEY, passed INTEGER NOT NULL, problems TEXT NOT NULL, ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS circuits (  -- a stage paused because a dependency is unreachable
  stage TEXT PRIMARY KEY, dependency TEXT NOT NULL, opened REAL NOT NULL,
  probe_at REAL NOT NULL, failures INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value REAL NOT NULL);
"""

HEARTBEAT_STALE_S = 30   # no supervisor tick for this long: the host was asleep or frozen
PROBE_MIN_S, PROBE_MAX_S = 15, 300  # circuit breaker probe interval: 15 s doubling to 5 min


@dataclass
class Task:
    id: str
    stage: str
    payload: dict
    attempts: int
    worker: str
    claimed_at: float = 0.0


class TaskQueue:
    def __init__(self, path: str, backoff_base: float = 2.0):
        self.path = path
        self.backoff_base = backoff_base
        with self._db() as conn:
            conn.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextmanager
    def _db(self):
        conn = self._conn()
        try:
            yield conn
        finally:
            conn.close()

    # ── producer side ────────────────────────────────────────────────────────────
    def enqueue(self, stage: str, tasks: list[tuple[str, dict]], max_attempts: int) -> int:
        """INSERT OR IGNORE: re-planning after a crash never duplicates or resets work."""
        now = time.time()
        with self._db() as conn:
            before = conn.total_changes
            conn.executemany(
                "INSERT OR IGNORE INTO tasks (id, stage, payload, max_attempts, updated) "
                "VALUES (?, ?, ?, ?, ?)",
                [(tid, stage, json.dumps(p), max_attempts, now) for tid, p in tasks],
            )
            return conn.total_changes - before

    # ── worker side ──────────────────────────────────────────────────────────────
    def claim(self, stage: str, worker: str, lease_s: float) -> Task | None:
        now = time.time()
        conn = self._conn()
        try:
            conn.execute("BEGIN IMMEDIATE")  # one claimer at a time; claims are tiny
            circuit = conn.execute(
                "SELECT probe_at, failures FROM circuits WHERE stage=?", (stage,)).fetchone()
            if circuit:
                if now < circuit[0]:
                    conn.execute("COMMIT")
                    return None  # paused: the dependency is down, nothing to do but wait
                # Half-open: this claim is the probe. Push probe_at forward so only one
                # worker probes at a time; success closes the circuit, failure reopens it.
                conn.execute("UPDATE circuits SET probe_at=? WHERE stage=?",
                             (now + _probe_delay(circuit[1]), stage))
            # Wall-clock leases all look expired after the laptop sleeps. Until the supervisor
            # has ticked again (and extended them), nobody reclaims: the worker holding that
            # lease is probably alive and mid-task, just as frozen as everyone else was.
            hb = conn.execute("SELECT value FROM meta WHERE key='heartbeat'").fetchone()
            reclaim_before = now if (hb is None or now - hb[0] < HEARTBEAT_STALE_S) else 0
            row = conn.execute(
                "SELECT id, payload, attempts, status, worker, max_attempts FROM tasks "
                "WHERE stage = ? AND ((status = 'pending' AND not_before <= ?) "
                "   OR (status = 'running' AND lease_until < ?)) "
                "ORDER BY id LIMIT 1",
                (stage, now, reclaim_before),
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            tid, payload, attempts, status, old_worker, max_attempts = row
            if status == "running":
                # Its worker died or hung. The crash counts as an attempt, so a task that
                # kills every worker it touches (a poison pill) still ends up dead-lettered.
                self._event(conn, tid, old_worker, "lease_expired", f"reclaimed by {worker}")
                if attempts >= max_attempts:
                    conn.execute(
                        "UPDATE tasks SET status='dead', last_error=?, updated=? WHERE id=?",
                        ("worker crashed on every attempt (poison pill?)", now, tid),
                    )
                    self._event(conn, tid, worker, "dead", "lease expired on final attempt")
                    conn.execute("COMMIT")
                    return self.claim(stage, worker, lease_s)
            conn.execute(
                "UPDATE tasks SET status='running', attempts=attempts+1, lease_until=?, "
                "worker=?, updated=? WHERE id=?",
                (now + lease_s, worker, now, tid),
            )
            conn.execute("COMMIT")
            return Task(tid, stage, json.loads(payload), attempts + 1, worker, now)
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def complete(self, task: Task, result: dict) -> bool:
        """Fenced: only the worker that holds the lease may complete. A worker that hung past
        its lease (and was replaced) gets False back and its result is discarded."""
        with self._db() as conn:
            cur = conn.execute(
                "UPDATE tasks SET status='done', result=?, lease_until=NULL, updated=? "
                "WHERE id=? AND worker=? AND status='running'",
                (json.dumps(result), time.time(), task.id, task.worker),
            )
            ok = cur.rowcount == 1
            self._event(conn, task.id, task.worker, "done" if ok else "fenced", None)
            if ok:
                self._close_circuit(conn, task.stage, task.claimed_at)
            return ok

    def offline(self, task: Task, dependency: str, error: str) -> None:
        """The task didn't fail, the world did. Hand the task back WITHOUT spending an attempt
        and pause the whole stage. Before this existed, the DNS failures after one night of
        laptop sleep burned 11 attempts, and a 30-minute router outage would have
        dead-lettered every task in flight."""
        now = time.time()
        with self._db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE tasks SET status='pending', attempts=attempts-1, not_before=0, "
                "lease_until=NULL, last_error=?, updated=? WHERE id=? AND worker=?",
                (error, now, task.id, task.worker),
            )
            if cur.rowcount:
                self._event(conn, task.id, task.worker, "offline", error[:300])
            row = conn.execute("SELECT failures FROM circuits WHERE stage=?", (task.stage,)).fetchone()
            if row is None:
                conn.execute("INSERT INTO circuits VALUES (?, ?, ?, ?, 1)",
                             (task.stage, dependency, now, now + _probe_delay(1)))
                self._event(conn, None, task.worker, "circuit_open", f"{task.stage}: {dependency} unreachable")
            else:
                conn.execute("UPDATE circuits SET failures=failures+1, probe_at=? WHERE stage=?",
                             (now + _probe_delay(row[0] + 1), task.stage))
            conn.execute("COMMIT")

    def _close_circuit(self, conn, stage: str, claimed_at: float):
        row = conn.execute("SELECT dependency, opened FROM circuits WHERE stage=?", (stage,)).fetchone()
        # Only a task claimed *after* the outage began proves the dependency is back. A task
        # that started before it (often against a different host) proves nothing; the first
        # blackout drill showed "back after 0s" for exactly that reason.
        if row and claimed_at >= row[1]:
            conn.execute("DELETE FROM circuits WHERE stage=?", (stage,))
            self._event(conn, None, None, "circuit_closed",
                        f"{stage}: {row[0]} back after {time.time() - row[1]:.0f}s")

    def circuit(self, stage: str) -> tuple[str, float] | None:
        with self._db() as conn:
            return conn.execute("SELECT dependency, opened FROM circuits WHERE stage=?", (stage,)).fetchone()

    # ── sleep detection ──────────────────────────────────────────────────────────
    def beat(self, now: float | None = None):
        with self._db() as conn:
            conn.execute("INSERT OR REPLACE INTO meta VALUES ('heartbeat', ?)", (now or time.time(),))

    def extend_leases(self, seconds: float) -> int:
        """The host slept for `seconds`. Give every running task that much more time: its
        worker didn't run either, so the lease shouldn't have run out."""
        with self._db() as conn:
            return conn.execute(
                "UPDATE tasks SET lease_until = lease_until + ? WHERE status='running'", (seconds,)
            ).rowcount

    def fail(self, task: Task, error: str, retryable: bool) -> str:
        now = time.time()
        with self._db() as conn:
            row = conn.execute(
                "SELECT max_attempts FROM tasks WHERE id=? AND worker=?", (task.id, task.worker)
            ).fetchone()
            if row is None:
                return "fenced"
            if retryable and task.attempts < row[0]:
                delay = self.backoff_base ** task.attempts  # 2s, 4s, 8s, ...
                conn.execute(
                    "UPDATE tasks SET status='pending', not_before=?, lease_until=NULL, "
                    "last_error=?, updated=? WHERE id=?",
                    (now + delay, error, now, task.id),
                )
                outcome = "retry"
            else:
                conn.execute(
                    "UPDATE tasks SET status='dead', lease_until=NULL, last_error=?, updated=? "
                    "WHERE id=?",
                    (error, now, task.id),
                )
                outcome = "dead"
            self._event(conn, task.id, task.worker, outcome, error[:300])
            return outcome

    # ── supervisor / report side ─────────────────────────────────────────────────
    def event(self, kind: str, detail: str, task_id: str | None = None, worker: str | None = None):
        with self._db() as conn:
            self._event(conn, task_id, worker, kind, detail)

    @staticmethod
    def _event(conn, task_id, worker, kind, detail):
        conn.execute(
            "INSERT INTO events (ts, task_id, worker, kind, detail) VALUES (?, ?, ?, ?, ?)",
            (time.time(), task_id, worker, kind, detail),
        )

    def counts(self, stage: str) -> dict[str, int]:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) FROM tasks WHERE stage=? GROUP BY status", (stage,)
            ).fetchall()
        return dict(rows)

    def drained(self, stage: str) -> bool:
        c = self.counts(stage)
        return c.get("pending", 0) == 0 and c.get("running", 0) == 0

    def results(self, stage: str) -> list[tuple[dict, dict]]:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT payload, result FROM tasks WHERE stage=? AND status='done' ORDER BY id",
                (stage,),
            ).fetchall()
        return [(json.loads(p), json.loads(r)) for p, r in rows]

    def history(self, task_id: str) -> list[str]:
        """Every failure a task had, oldest first. The last error alone lies: a batch that
        failed canaries three times and then hit a timeout looks 'transient'."""
        with self._db() as conn:
            rows = conn.execute(
                "SELECT detail FROM events WHERE task_id=? AND kind IN ('retry','dead') ORDER BY ts",
                (task_id,),
            ).fetchall()
        return [r[0] for r in rows]

    def dead(self, stage: str | None = None) -> list[dict]:
        q = "SELECT id, stage, attempts, last_error, payload FROM tasks WHERE status='dead'"
        args: tuple = ()
        if stage:
            q, args = q + " AND stage=?", (stage,)
        with self._db() as conn:
            rows = conn.execute(q + " ORDER BY id", args).fetchall()
        return [
            {"id": i, "stage": s, "attempts": a, "error": e, "payload": json.loads(p)}
            for i, s, a, e, p in rows
        ]

    def requeue(self, task_id: str) -> bool:
        """Human (or Claude, in the morning) decided a dead task deserves another go."""
        with self._db() as conn:
            cur = conn.execute(
                "UPDATE tasks SET status='pending', attempts=0, not_before=0, updated=? "
                "WHERE id=? AND status='dead'",
                (time.time(), task_id),
            )
            if cur.rowcount:
                self._event(conn, task_id, "human", "requeued", None)
            return cur.rowcount == 1

    def record_gate(self, stage: str, problems: list[str]):
        with self._db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO gates (stage, passed, problems, ts) VALUES (?, ?, ?, ?)",
                (stage, int(not problems), json.dumps(problems), time.time()),
            )

    def gates(self) -> dict[str, tuple[bool, list[str]]]:
        with self._db() as conn:
            rows = conn.execute("SELECT stage, passed, problems FROM gates").fetchall()
        return {s: (bool(p), json.loads(pr)) for s, p, pr in rows}

    def events(self, kinds: tuple[str, ...]) -> list[tuple]:
        marks = ",".join("?" for _ in kinds)
        with self._db() as conn:
            return conn.execute(
                f"SELECT ts, task_id, worker, kind, detail FROM events WHERE kind IN ({marks}) "
                "ORDER BY ts",
                kinds,
            ).fetchall()

    def statuses(self, task_ids: set[str]) -> dict[str, str]:
        with self._db() as conn:
            return {tid: st for tid, st in conn.execute("SELECT id, status FROM tasks") if tid in task_ids}

    def attempts_histogram(self) -> dict[str, dict[int, int]]:
        with self._db() as conn:
            rows = conn.execute(
                "SELECT stage, attempts, COUNT(*) FROM tasks GROUP BY stage, attempts"
            ).fetchall()
        out: dict[str, dict[int, int]] = {}
        for stage, attempts, n in rows:
            out.setdefault(stage, {})[attempts] = n
        return out


def _probe_delay(failures: int) -> float:
    return min(PROBE_MAX_S, PROBE_MIN_S * 2 ** (failures - 1))
