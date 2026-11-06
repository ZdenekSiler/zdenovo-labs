"""Read-only access to the blog database, enforced three times over.

1. The connection is opened `mode=ro` (or `immutable=1` for a static snapshot).
2. `PRAGMA query_only = ON` refuses writes even if (1) were somehow bypassed.
3. A SQLite authorizer allows only reads of listed tables and columns. Everything else,
   including comment bodies, PRAGMA, ATTACH and every write, is refused by SQLite itself,
   before a query runs. A bug in a query function can't leak what the authorizer denies.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

# table -> readable columns ("*" = all). Comments are readable only for counting.
DEFAULT_ALLOW: dict[str, frozenset[str] | str] = {
    "posts": "*",
    "series": "*",
    "api_costs": "*",
    "comments": frozenset({"post_slug", "status", ""}),  # "" = count(*) / rowid access
    "posts_fts": "*",
}

SAFE_FUNCTIONS = frozenset({
    "count", "sum", "avg", "min", "max", "total", "round", "length", "lower", "upper",
    "coalesce", "ifnull", "nullif", "substr", "instr", "date", "datetime", "strftime",
    "julianday", "abs", "cast", "json_array_length", "json_extract", "json_each",
    "snippet", "bm25", "highlight", "match", "printf", "replace", "trim", "iif",
})


class ReadOnlyViolation(Exception):
    """Raised (as a sqlite3.DatabaseError) when the authorizer refuses something."""


@dataclass
class Policy:
    allow: dict[str, frozenset[str] | str] = field(default_factory=lambda: dict(DEFAULT_ALLOW))
    max_rows: int = 200
    max_seconds: float = 2.0
    denied: list[str] = field(default_factory=list)  # what the authorizer refused, for errors

    def authorize(self, action: int, arg1: str | None, arg2: str | None, db: str | None,
                  source: str | None) -> int:
        if action == sqlite3.SQLITE_SELECT:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ:
            table, column = arg1 or "", arg2 or ""
            if table.startswith("posts_fts_"):
                return sqlite3.SQLITE_OK  # FTS5 shadow tables (index pages, config)
            # Not sqlite_master: SQLite reports a user's "SELECT FROM sqlite_master" exactly like
            # an internal read, and the schema of tables we don't expose is none of the model's business.
            cols = self.allow.get(table)
            if cols == "*" or (cols is not None and column in cols):
                return sqlite3.SQLITE_OK
            self.denied.append(f"read {table}.{column}" if column else f"read {table}")
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_FUNCTION:
            if (arg2 or "").lower() in SAFE_FUNCTIONS:
                return sqlite3.SQLITE_OK
            self.denied.append(f"function {arg2}")
            return sqlite3.SQLITE_DENY
        if action == sqlite3.SQLITE_PRAGMA and arg1 == "data_version" and arg2 is None:
            return sqlite3.SQLITE_OK  # FTS5 runs this internally to check for changes; read-only
        if action == sqlite3.SQLITE_RECURSIVE:  # WITH RECURSIVE is fine for reads
            return sqlite3.SQLITE_OK
        self.denied.append(_ACTION_NAMES.get(action, f"action {action}"))
        return sqlite3.SQLITE_DENY


# Authorizer action codes only. (Building this from every SQLITE_* int is wrong: SQLITE_ATTACH
# shares its value with the SQLITE_FORMAT error code, and the lab printed "format".)
_ACTION_NAMES = {getattr(sqlite3, f"SQLITE_{n.upper()}"): n for n in (
    "create_index", "create_table", "create_temp_index", "create_temp_table",
    "create_temp_trigger", "create_temp_view", "create_trigger", "create_view", "delete",
    "drop_index", "drop_table", "drop_temp_index", "drop_temp_table", "drop_temp_trigger",
    "drop_temp_view", "drop_trigger", "drop_view", "insert", "pragma", "transaction", "update",
    "attach", "detach", "alter_table", "reindex", "analyze", "create_vtable", "drop_vtable",
    "savepoint")}


class BlogStore:
    """One read-only connection with a policy. Create one per server; it isn't shared state."""

    def __init__(self, path: str | Path, *, immutable: bool = False, policy: Policy | None = None):
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(f"no database at {self.path}")
        flag = "immutable=1" if immutable else "mode=ro"
        self.conn = sqlite3.connect(f"file:{self.path}?{flag}", uri=True, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA query_only = ON")  # before the authorizer, which forbids PRAGMA
        self.policy = policy or Policy()
        self.conn.set_authorizer(self.policy.authorize)

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        """Run one statement with a row cap and a wall-clock limit."""
        self.policy.denied.clear()
        deadline = time.monotonic() + self.policy.max_seconds
        self.conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
        try:
            cur = self.conn.execute(sql, params)
            rows = cur.fetchmany(self.policy.max_rows + 1)
        except sqlite3.DatabaseError as e:
            if self.policy.denied:
                raise ReadOnlyViolation(f"refused by policy: {', '.join(dict.fromkeys(self.policy.denied))}") from e
            if "interrupted" in str(e):
                raise ReadOnlyViolation(f"query exceeded {self.policy.max_seconds}s") from e
            raise
        finally:
            self.conn.set_progress_handler(None, 0)
        if len(rows) > self.policy.max_rows:
            raise ReadOnlyViolation(f"query returned more than {self.policy.max_rows} rows; add LIMIT")
        return rows

    def close(self) -> None:
        self.conn.close()
