# MCP in Practice: the lab

Companion code for the 6-part series **"MCP in Practice: Build, Sandbox and Use a Real MCP
Server in Python"** on [zdenovo.com](https://zdenovo.com/series/mcp-in-practice): a read-only
analytics MCP server over a blog's SQLite database, run inside a locked-down Docker sandbox.
Written against MCP spec `2026-07-28` and the Python SDK `mcp` **2.3.0**.

```bash
./run_all.sh                         # builds the synthetic blog DB, runs every offline demo + 23 tests
uv run python -m blogmcp.server data/fixture.db     # the server, over stdio
sandbox/build.sh && sandbox/run.sh data/fixture.db  # the same server, sandboxed (needs Docker)
```

No API key is needed for any of that. The data is **synthetic** (`blogmcp/fixture.py`: invented
posts, views, comments and costs, generated deterministically). The articles also quote runs
against a sanitised snapshot of the author's real blog, labelled `[snapshot]`; that snapshot
isn't in this repo. `outputs/` holds the captured runs the articles quote.

| Path | What | Part |
|---|---|---|
| `demos/hello_blog.py` | the 20-line first server | 1 |
| `blogmcp/store.py` | read-only SQLite: `mode=ro` + `query_only` + an authorizer allowlist | 2 |
| `blogmcp/analytics.py` | the questions, as typed methods, no MCP | 2 |
| `blogmcp/server.py` | `build_server()`: 6 tools, 3 resources, 2 prompts; `--http PORT` for Streamable HTTP | 2, 3, 6 |
| `blogmcp/sql_tool.py` | `query_blog`, naive vs guarded (`BLOG_SQL_TOOL=guarded`) | 3 |
| `blogmcp/fixture.py`, `schema.sql` | the synthetic DB, with the real blog's schema | all |
| `blogmcp/snapshot.py` | how the real-data snapshot was sanitised (comment text blanked) | 2 |
| `sandbox/Dockerfile`, `run.sh`, `build.sh` | digest-pinned image; the locked-down `docker run` | 4 |
| `sandbox/naughty_server.py` | a probe server that tries what a malicious one would. Harmless: it lists names only and rolls back its DB write | 4 |
| `live/host.py` | an 80-line MCP host: Claude + the sandboxed server. **Costs money** (cents) | 5 |
| `live/claude_code.sh` | headless Claude Code with only this server, `--max-budget-usd 0.40` | 5 |
| `tests/` | 23 behaviour tests; the sandbox escape test runs when the image exists | all |

Live runs (part 5) are not in `run_all.sh`:

```bash
ANTHROPIC_API_KEY=... uv run --group live python live/host.py data/fixture.db "Which article is most read?"
live/claude_code.sh data/fixture.db "Which article is most read?"     # uses your Claude Code login
```

To point it at your own SQLite database, adapt the queries in `analytics.py` to your schema and
update the authorizer allowlist in `store.py` (`DEFAULT_ALLOW`) to exactly the tables and columns
the server may read.
Captured with CPython 3.14.4 (host) / 3.12.16 (container), `mcp` 2.3.0, `anthropic` 2.6.0, Docker 29.4 rootless.
