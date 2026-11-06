"""What the series claims, pinned down. Offline on the fixture; the sandbox test needs Docker."""

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import anyio
import pytest
from mcp import Client, StdioServerParameters

from blogmcp.fixture import build
from blogmcp.server import build_server
from blogmcp.store import BlogStore, ReadOnlyViolation

LAB = Path(__file__).resolve().parents[1]
FIXTURE = LAB / "data" / "fixture.db"
COMMENT_TEXT = "Synthetic comment body"


@pytest.fixture(scope="session", autouse=True)
def fixture_db():
    if not FIXTURE.exists():
        build(FIXTURE)


def call(tool, args=None, **kw):
    async def go():
        async with Client(build_server(str(FIXTURE), **kw)) as c:
            return await c.call_tool(tool, args or {})
    return anyio.run(go)


# --- the store ---------------------------------------------------------------------------

@pytest.mark.parametrize("sql", ["UPDATE posts SET views = 0", "DELETE FROM posts", "PRAGMA table_info(posts)",
                                 "ATTACH DATABASE '/tmp/x.db' AS x", "SELECT body FROM comments",
                                 "SELECT name FROM sqlite_master", "SELECT load_extension('x')"])
def test_store_refuses(sql):
    with pytest.raises(ReadOnlyViolation):
        BlogStore(FIXTURE).query(sql)


def test_store_refuses_unlisted_tables(tmp_path):
    db = tmp_path / "blog.db"
    shutil.copy(FIXTURE, db)
    c = sqlite3.connect(db)
    c.execute("CREATE TABLE drafts (id TEXT, content TEXT)")
    c.commit()
    c.close()
    with pytest.raises(ReadOnlyViolation, match="read drafts"):
        BlogStore(db).query("SELECT * FROM drafts")


def test_store_allows_counts_and_fts():
    s = BlogStore(FIXTURE)
    assert s.query("SELECT count(*) n FROM comments")[0]["n"] > 0
    assert s.query("SELECT slug FROM posts_fts WHERE posts_fts MATCH 'injection'")


def test_row_cap():
    from blogmcp.store import Policy
    s = BlogStore(FIXTURE, policy=Policy(max_rows=5))
    with pytest.raises(ReadOnlyViolation, match="more than 5 rows"):
        s.query("SELECT slug FROM posts")


# --- the tools ---------------------------------------------------------------------------

def test_tool_listing_is_read_only_and_typed():
    async def go():
        async with Client(build_server(str(FIXTURE))) as c:
            return (await c.list_tools()).tools
    tools = anyio.run(go)
    assert {t.name for t in tools} == {"top_articles", "article_stats", "search_articles", "list_series",
                                       "series_report", "blog_summary"}
    assert all(t.output_schema and t.annotations.read_only_hint for t in tools)


def test_top_articles_sorted_and_bounded():
    arts = call("top_articles", {"limit": 5}).structured_content["articles"]
    assert [a["views"] for a in arts] == sorted((a["views"] for a in arts), reverse=True)
    assert call("top_articles", {"limit": 21}).is_error


def test_unknown_slug_error_reaches_the_model():
    r = call("article_stats", {"slug": "notes-on-prompt-cachin"})
    assert r.is_error and "notes-on-prompt-caching" in r.content[0].text


def test_cost_per_view_skips_hand_written():
    db = Path(FIXTURE).with_name("zero.db")
    shutil.copy(FIXTURE, db)
    c = sqlite3.connect(db)
    c.execute("UPDATE posts SET gen_cost_usd = 0 WHERE slug = 'notes-on-redis'")
    c.commit()
    c.close()
    async def go():
        async with Client(build_server(str(db))) as cl:
            return await cl.call_tool("top_articles", {"metric": "cost_per_view", "limit": 20})
    slugs = [a["slug"] for a in anyio.run(go).structured_content["articles"]]
    db.unlink()
    assert "notes-on-redis" not in slugs


@pytest.mark.parametrize("q", ["C++ AND", '"unbalanced', "NEAR(x y)", "rate-limiting*"])
def test_search_survives_fts_syntax(q):
    assert not call("search_articles", {"query": q}).is_error


def test_no_tool_or_resource_leaks_comment_text():
    async def go():
        out = []
        async with Client(build_server(str(FIXTURE))) as c:
            for name, args in (("top_articles", {"limit": 20}), ("blog_summary", {}), ("list_series", {}),
                               ("article_stats", {"slug": "notes-on-prompt-caching"}),
                               ("search_articles", {"query": "synthetic comment body"}),
                               ("series_report", {"series_id": "observability-101"})):
                out.append(json.dumps((await c.call_tool(name, args)).structured_content))
            out.append((await c.read_resource("blog://schema")).contents[0].text)
        return out
    assert not any(COMMENT_TEXT in o for o in anyio.run(go))


def test_resource_template_and_prompts():
    async def go():
        async with Client(build_server(str(FIXTURE))) as c:
            post = (await c.read_resource("blog://posts/notes-on-redis")).contents[0].text
            prompts = [p.name for p in (await c.list_prompts()).prompts]
            return post, prompts
    post, prompts = anyio.run(go)
    assert post.startswith("# Notes on Redis") and set(prompts) == {"weekly_report", "content_gap_analysis"}


def test_naive_sql_leaks_and_guarded_refuses():
    q = {"sql": "SELECT body FROM comments LIMIT 1"}
    assert COMMENT_TEXT in json.dumps(call("query_blog", q, sql="naive").structured_content)
    r = call("query_blog", q, sql="guarded")
    assert r.is_error and "refused by policy" in r.content[0].text


def test_fixture_is_deterministic(tmp_path):
    build(tmp_path / "a.db")
    build(tmp_path / "b.db")
    rows = lambda p: sqlite3.connect(p).execute("SELECT * FROM posts ORDER BY slug").fetchall()  # noqa: E731
    assert rows(tmp_path / "a.db") == rows(tmp_path / "b.db")


# --- the sandbox ----------------------------------------------------------------------------

def _docker_ready() -> bool:
    if not shutil.which("docker"):
        return False
    return subprocess.run(["docker", "image", "inspect", "blogmcp:lab"], capture_output=True).returncode == 0


@pytest.mark.skipif(not _docker_ready(), reason="needs docker and the blogmcp:lab image (sandbox/build.sh)")
def test_sandbox_blocks_every_escape():
    async def go():
        params = StdioServerParameters(command=str(LAB / "sandbox" / "run.sh"), args=[str(FIXTURE), "naughty"])
        async with Client(params) as c:
            return (await c.call_tool("try_everything", {})).structured_content["result"]
    res = {p["attempt"]: p for p in anyio.run(go)}
    allowed = {k for k, p in res.items() if p["allowed"]}
    assert allowed <= {"identity", "see secret-looking env vars"}
    assert "uid=10001" in res["identity"]["detail"]
    if "see secret-looking env vars" in allowed:  # only the base image's public GPG key id
        assert res["see secret-looking env vars"]["detail"] == "1 visible: GPG_KEY"
