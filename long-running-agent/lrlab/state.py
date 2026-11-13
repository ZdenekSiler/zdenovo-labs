"""Saved state: the single source of truth. Every app, worker and delivery reads from here.

One SQLite file. Tables:
  conversations  status, lease and fence per conversation
  inbound        every accepted message, deduplicated by idempotency key
  events         append-only log (public events are what apps show; private ones are for you)
  checkpoints    the harness's message history + usage, written after every tool result
  tool_journal   tool results by call id, so a resumed run replays instead of re-executing
  vault          secrets entered by the user; the transcript only ever holds a handle
  schedules      scheduler state (see scheduler.py)
  cursors        per-delivery read position in the event log
"""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    thread_key TEXT UNIQUE NOT NULL,
    status TEXT NOT NULL DEFAULT 'idle',
    waiting_for TEXT,
    lease_owner TEXT,
    lease_until REAL,
    fence INTEGER NOT NULL DEFAULT 0,
    retry_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS inbound (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    conv_id TEXT NOT NULL,
    idem_key TEXT UNIQUE NOT NULL,
    channel TEXT NOT NULL,
    sender TEXT NOT NULL,
    provenance TEXT NOT NULL,
    content TEXT NOT NULL,
    received_at REAL NOT NULL,
    consumed_at REAL
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    conv_id TEXT,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    visibility TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checkpoints (
    conv_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tool_journal (
    conv_id TEXT NOT NULL,
    call_id TEXT NOT NULL,
    tool TEXT NOT NULL,
    args TEXT NOT NULL,
    result TEXT NOT NULL,
    ts REAL NOT NULL,
    PRIMARY KEY (conv_id, call_id)
);
CREATE TABLE IF NOT EXISTS vault (
    name TEXT PRIMARY KEY,
    conv_id TEXT NOT NULL,
    value TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS schedules (
    id TEXT PRIMARY KEY,
    at_utc TEXT NOT NULL,
    thread_key TEXT NOT NULL,
    prompt TEXT NOT NULL,
    catchup TEXT NOT NULL,
    jitter_s INTEGER NOT NULL,
    last_fired TEXT
);
CREATE TABLE IF NOT EXISTS cursors (
    name TEXT PRIMARY KEY,
    seq INTEGER NOT NULL
);
"""


class LeaseLost(Exception):
    """A newer worker owns this conversation. The old one must stop writing, right now."""


class StateStore:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # autocommit mode; multi-statement writes use explicit BEGIN IMMEDIATE
        self.db = sqlite3.connect(db_path, isolation_level=None, timeout=30, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.executescript(SCHEMA)

    # ── conversations ────────────────────────────────────────────────────────
    def conversation_for(self, thread_key: str) -> str:
        row = self.db.execute("SELECT id FROM conversations WHERE thread_key=?", (thread_key,)).fetchone()
        if row:
            return row["id"]
        conv_id = "conv_" + uuid.uuid5(uuid.NAMESPACE_URL, thread_key).hex[:12]
        now = time.time()
        self.db.execute(
            "INSERT OR IGNORE INTO conversations (id, thread_key, created_at, updated_at) VALUES (?,?,?,?)",
            (conv_id, thread_key, now, now),
        )
        return conv_id

    def conversation(self, conv_id: str) -> dict[str, Any]:
        row = self.db.execute("SELECT * FROM conversations WHERE id=?", (conv_id,)).fetchone()
        out = dict(row)
        out["waiting_for"] = json.loads(out["waiting_for"]) if out["waiting_for"] else None
        return out

    def set_waiting(self, conv_id: str, waiting_for: dict | None) -> None:
        self.db.execute(
            "UPDATE conversations SET waiting_for=?, updated_at=? WHERE id=?",
            (json.dumps(waiting_for) if waiting_for else None, time.time(), conv_id),
        )

    # ── inbound ──────────────────────────────────────────────────────────────
    def add_inbound(self, conv_id: str, idem_key: str, channel: str, sender: str,
                    provenance: str, content: str) -> bool:
        cur = self.db.execute(
            "INSERT OR IGNORE INTO inbound (conv_id, idem_key, channel, sender, provenance, content, received_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (conv_id, idem_key, channel, sender, provenance, content, time.time()),
        )
        return cur.rowcount == 1

    def unconsumed_inbound(self, conv_id: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM inbound WHERE conv_id=? AND consumed_at IS NULL ORDER BY seq", (conv_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ── events ───────────────────────────────────────────────────────────────
    def event(self, conv_id: str | None, kind: str, payload: dict, visibility: str = "private") -> int:
        cur = self.db.execute(
            "INSERT INTO events (conv_id, ts, kind, visibility, payload) VALUES (?,?,?,?,?)",
            (conv_id, time.time(), kind, visibility, json.dumps(payload, ensure_ascii=False)),
        )
        return cur.lastrowid

    def events_after(self, seq: int, conv_id: str | None = None, public_only: bool = True) -> list[dict]:
        sql = "SELECT * FROM events WHERE seq > ?"
        args: list[Any] = [seq]
        if conv_id:
            sql += " AND conv_id = ?"
            args.append(conv_id)
        if public_only:
            sql += " AND visibility = 'public'"
        rows = self.db.execute(sql + " ORDER BY seq", args).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def cursor(self, name: str) -> int:
        row = self.db.execute("SELECT seq FROM cursors WHERE name=?", (name,)).fetchone()
        return row["seq"] if row else 0

    def set_cursor(self, name: str, seq: int) -> None:
        self.db.execute("INSERT OR REPLACE INTO cursors (name, seq) VALUES (?,?)", (name, seq))

    # ── checkpoints (fenced) ─────────────────────────────────────────────────
    def load_checkpoint(self, conv_id: str) -> dict | None:
        row = self.db.execute("SELECT state FROM checkpoints WHERE conv_id=?", (conv_id,)).fetchone()
        return json.loads(row["state"]) if row else None

    def save_checkpoint(self, conv_id: str, fence: int, state: dict, consume_seqs: list[int] = ()) -> None:
        """Write the checkpoint only if we still hold the fence. Optionally mark inbound consumed
        in the same transaction, so a message is never both lost and unprocessed."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT fence FROM conversations WHERE id=?", (conv_id,)).fetchone()
            if row is None or row["fence"] != fence:
                raise LeaseLost(f"{conv_id}: fence {fence} is stale (current {row and row['fence']})")
            now = time.time()
            self.db.execute(
                "INSERT OR REPLACE INTO checkpoints (conv_id, state, updated_at) VALUES (?,?,?)",
                (conv_id, json.dumps(state, ensure_ascii=False), now),
            )
            for seq in consume_seqs:
                self.db.execute("UPDATE inbound SET consumed_at=? WHERE seq=?", (now, seq))
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    # ── tool journal ─────────────────────────────────────────────────────────
    def journal_get(self, conv_id: str, call_id: str) -> dict | None:
        row = self.db.execute(
            "SELECT result FROM tool_journal WHERE conv_id=? AND call_id=?", (conv_id, call_id)
        ).fetchone()
        return json.loads(row["result"]) if row else None

    def journal_put(self, conv_id: str, call_id: str, tool: str, args: dict, result: dict) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO tool_journal (conv_id, call_id, tool, args, result, ts) VALUES (?,?,?,?,?,?)",
            (conv_id, call_id, tool, json.dumps(args), json.dumps(result), time.time()),
        )

    # ── vault ────────────────────────────────────────────────────────────────
    def vault_put(self, name: str, conv_id: str, value: str) -> str:
        self.db.execute(
            "INSERT OR REPLACE INTO vault (name, conv_id, value, created_at) VALUES (?,?,?,?)",
            (name, conv_id, value, time.time()),
        )
        return f"secret://{name}"

    def vault_get(self, handle: str) -> str | None:
        row = self.db.execute("SELECT value FROM vault WHERE name=?", (handle.removeprefix("secret://"),)).fetchone()
        return row["value"] if row else None
