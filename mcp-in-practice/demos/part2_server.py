"""Part 2: read-only by construction, then the first real tools (fixture DB)."""
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path

import anyio
from mcp import Client

from blogmcp.server import build_server
from blogmcp.store import BlogStore, ReadOnlyViolation

LAB = Path(__file__).resolve().parents[1]
FIXTURE = LAB / "data" / "fixture.db"
WRITE = "UPDATE posts SET views = views + 1000"


def layers() -> None:
    print("== 1. three independent read-only layers, each tested alone ==")
    tmp = Path(tempfile.mkdtemp()) / "blog.db"
    shutil.copy(FIXTURE, tmp)
    attempts = {
        "plain connection (no protection)": lambda: sqlite3.connect(tmp),
        "layer 1: mode=ro": lambda: sqlite3.connect(f"file:{tmp}?mode=ro", uri=True),
    }
    for name, open_ in attempts.items():
        c = open_()
        try:
            c.execute(WRITE)
            print(f"{name:40} write SUCCEEDED (rolled back)")
            c.rollback()
        except sqlite3.Error as e:
            print(f"{name:40} {type(e).__name__}: {e}")
    c = sqlite3.connect(tmp)
    c.execute("PRAGMA query_only = ON")
    try:
        c.execute(WRITE)
    except sqlite3.Error as e:
        print(f"{'layer 2: PRAGMA query_only (rw file)':40} {type(e).__name__}: {e}")
    store = BlogStore(tmp)
    for q in (WRITE, "SELECT author, body FROM comments", "ATTACH DATABASE '/tmp/x.db' AS x",
              "SELECT load_extension('evil')"):
        try:
            store.query(q)
        except ReadOnlyViolation as e:
            print(f"{'layer 3: authorizer':40} {q[:34]:35} -> {e}")
    shutil.rmtree(tmp.parent)


async def tools() -> None:
    print("\n== 2. the tools, through an in-process client ==")
    async with Client(build_server(str(FIXTURE))) as c:
        for t in (await c.list_tools()).tools:
            ro = t.annotations.read_only_hint if t.annotations else None
            print(f"{t.name:16} args={list(t.input_schema.get('properties', {}))!s:52} readOnlyHint={ro}")
        r = await c.call_tool("top_articles", {"limit": 3})
        print("\ntop_articles(limit=3):")
        for a in r.structured_content["articles"]:
            print(f"  {a['rank']}. {a['title']} — {a['views']} views")
        print("  note:", r.structured_content["note"])
        r = await c.call_tool("article_stats", {"slug": "observability-101-part-1"})
        print("\narticle_stats('observability-101-part-1'):")
        print(json.dumps(r.structured_content, indent=2))


layers()
anyio.run(tools)
