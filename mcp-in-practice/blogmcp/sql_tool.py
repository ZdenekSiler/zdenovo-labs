"""The `query_blog` tool: "just let the model write SQL", in two versions.

naive   : a read-only connection and nothing else. Can't write, can read *everything*.
guarded : the BlogStore policy, tightened: posts and series only, row cap, time limit.

Part 3 runs both against the same question to show why read-only isn't the same as safe.
"""

from __future__ import annotations

import sqlite3
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from .store import BlogStore, Policy, ReadOnlyViolation

SqlMode = Literal["naive", "guarded"] | None

GUARDED_ALLOW = {"posts": frozenset({"slug", "title", "date", "summary", "tags", "views", "reactions",
                                     "reactions_down", "series_id", "series_order", "category",
                                     "gen_cost_usd", ""}),
                 "series": "*"}


class QueryResult(BaseModel):
    columns: list[str]
    rows: list[list[str | int | float | None]]
    row_count: int


def register_sql_tool(mcp: MCPServer, db_path: str, *, mode: str, immutable: bool) -> None:
    flag = "immutable=1" if immutable else "mode=ro"
    if mode == "naive":
        conn = sqlite3.connect(f"file:{db_path}?{flag}", uri=True, check_same_thread=False)

        def run(sql: str) -> QueryResult:
            cur = conn.execute(sql)
            rows = cur.fetchmany(200)
            return QueryResult(columns=[d[0] for d in cur.description or []],
                               rows=[list(r) for r in rows], row_count=len(rows))
    elif mode == "guarded":
        store = BlogStore(db_path, immutable=immutable,
                          policy=Policy(allow=dict(GUARDED_ALLOW), max_rows=100, max_seconds=1.0))

        def run(sql: str) -> QueryResult:
            try:
                rows = store.query(sql)
            except ReadOnlyViolation as e:
                raise ToolError(f"{e}. Allowed: posts (metadata columns, not content) and series. "
                                 "Use the other tools for comments and article text.") from e
            cols = list(rows[0].keys()) if rows else []
            return QueryResult(columns=cols, rows=[list(r) for r in rows], row_count=len(rows))
    else:
        raise ValueError(f"unknown sql mode {mode!r}")

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    def query_blog(
        sql: Annotated[str, Field(max_length=2000, description="One SQLite SELECT statement.")],
    ) -> QueryResult:
        """Run a read-only SQL query against the blog database."""
        return run(sql)
