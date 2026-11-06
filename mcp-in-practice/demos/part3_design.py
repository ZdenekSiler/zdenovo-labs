"""Part 3: designing tools for a model (fixture DB)."""
import json
import sqlite3
from pathlib import Path

import anyio
from mcp import Client
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from blogmcp.server import build_server

LAB = Path(__file__).resolve().parents[1]
FIXTURE = str(LAB / "data" / "fixture.db")


async def errors() -> None:
    print("== 1. an error message the model never sees ==")
    m = MCPServer("errors")

    @m.tool()
    def with_value_error(slug: str) -> str:
        """Look up an article."""
        raise ValueError(f"No article {slug!r}. Did you mean 'notes-on-prompt-caching'?")

    @m.tool()
    def with_tool_error(slug: str) -> str:
        """Look up an article."""
        raise ToolError(f"No article {slug!r}. Did you mean 'notes-on-prompt-caching'?")

    async with Client(m) as c:
        for name in ("with_value_error", "with_tool_error"):
            r = await c.call_tool(name, {"slug": "prompt-cachin"})
            print(f"{name:17} is_error={r.is_error} -> {r.content[0].text!r}")


async def validation() -> None:
    print("\n== 2. the schema says no before your code runs ==")
    async with Client(build_server(FIXTURE)) as c:
        for args in ({"limit": 50}, {"metric": "popularity"}, {"published_since": "last week"}):
            r = await c.call_tool("top_articles", args)
            first = r.content[0].text.splitlines()
            print(f"top_articles({json.dumps(args)}) -> is_error={r.is_error}: {first[1].strip()} / {first[2].strip()[:70]}")


async def semantics() -> None:
    print("\n== 3. the data has meaning your tool must encode ==")
    db = sqlite3.connect(f"file:{FIXTURE}?mode=ro", uri=True)
    naive = db.execute("SELECT title, gen_cost_usd, views FROM posts WHERE gen_cost_usd IS NOT NULL AND views > 0 "
                       "ORDER BY gen_cost_usd * 1.0 / views LIMIT 3").fetchall()
    print("naive cost-per-view ranking:", naive)
    print("(the real blog stores 0 for hand-written posts, so they 'won' at $0.00 per view; "
          "the tool now ranks gen_cost_usd > 0 only)")
    async with Client(build_server(FIXTURE)) as c:
        r = await c.call_tool("top_articles", {"metric": "cost_per_view", "limit": 3})
        print("tool note:", r.structured_content["note"].split("; ", 1)[1])


async def sizes() -> None:
    print("\n== 4. what each call costs the model's context ==")
    async with Client(build_server(FIXTURE)) as c:
        tools = (await c.list_tools()).tools
        listing = json.dumps([t.model_dump(exclude_none=True, by_alias=True) for t in tools])
        sent = json.dumps([{"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in tools])
        print(f"tools/list: {len(tools)} tools, {len(listing):,} chars as listed; {len(sent):,} chars of it go to the model "
              f"(name, description, input schema) on EVERY request")
        for name, args in (("top_articles", {"limit": 20}), ("article_stats", {"slug": "notes-on-redis"}),
                           ("search_articles", {"query": "cache", "limit": 10}), ("list_series", {}),
                           ("series_report", {"series_id": "rust-for-pythonistas"}), ("blog_summary", {})):
            r = await c.call_tool(name, args)
            body = json.dumps(r.structured_content)
            print(f"  {name:16} {json.dumps(args):42} {len(body):>6,} chars")
        r = await c.read_resource("blog://posts/notes-on-redis")
        print(f"  {'resource':16} {'blog://posts/notes-on-redis':42} {len(r.contents[0].text):>6,} chars")


async def raw_sql() -> None:
    print("\n== 5. 'just give it SQL': read-only is not the same as safe ==")
    q_private = "SELECT author, body FROM comments LIMIT 2"
    q_write = "DELETE FROM posts"
    q_fine = "SELECT series_id, sum(views) v FROM posts WHERE series_id IS NOT NULL GROUP BY 1 ORDER BY v DESC LIMIT 2"
    for mode in ("naive", "guarded"):
        async with Client(build_server(FIXTURE, sql=mode)) as c:
            for q in (q_fine, q_private, q_write):
                r = await c.call_tool("query_blog", {"sql": q})
                out = json.dumps(r.structured_content["rows"]) if not r.is_error else r.content[0].text
                print(f"{mode:7} {q[:42]:43} -> {'ERROR ' if r.is_error else ''}{out[:110]}")


async def main() -> None:
    for step in (errors, validation, semantics, sizes, raw_sql):
        await step()


anyio.run(main)
