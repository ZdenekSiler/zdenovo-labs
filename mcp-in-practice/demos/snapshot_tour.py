"""The real blog, from a sanitised snapshot (data/snapshot.db, not in the public repo).

Runs only where the snapshot exists. The articles quote this output, labelled [snapshot].
"""
import sys
from pathlib import Path

import anyio
from mcp import Client

from blogmcp.server import build_server

SNAP = Path(__file__).resolve().parents[1] / "data" / "snapshot.db"


async def main() -> None:
    async with Client(build_server(str(SNAP), immutable=True)) as c:
        s = (await c.call_tool("blog_summary", {})).structured_content
        print(f"== blog_summary ==\n{s['posts']} posts, {s['views']:,} views, {s['reactions_up']} reactions, "
              f"{s['comments']} published comments; generation cost ${s['generation_cost_usd']:.2f} "
              f"(${s['cost_per_1000_views_usd']:.2f} per 1,000 views); all API spend ${s['api_spend_usd']:.2f}")
        print("\n== top_articles(views, 5) ==")
        for a in (await c.call_tool("top_articles", {"limit": 5})).structured_content["articles"]:
            print(f"{a['rank']}. {a['views']:>4}  {a['published']}  {a['title']}")
        import sqlite3
        db = sqlite3.connect(f"file:{SNAP}?immutable=1", uri=True)
        print("\n== naive cost-per-view ranking (gen_cost_usd IS NOT NULL), i.e. the first version ==")
        for t, cost, v in db.execute("SELECT title, gen_cost_usd, views FROM posts WHERE gen_cost_usd IS NOT NULL "
                                     "AND views > 0 ORDER BY gen_cost_usd * 1.0 / views LIMIT 3"):
            print(f"${cost:.2f} generation cost, {v:>3} views  {t}")
        print("\n== top_articles(cost_per_view, 3) ==")
        for a in (await c.call_tool("top_articles", {"metric": "cost_per_view", "limit": 3})).structured_content["articles"]:
            print(f"{a['rank']}. ${a['value'] * 1000:.3f} per 1,000 views  ({a['views']} views)  {a['title']}")
        print("\n== list_series ==")
        for x in (await c.call_tool("list_series", {})).structured_content["series"]:
            print(f"{x['total_views']:>5} views  {x['parts']} parts  {x['series_id']}")
        print("\n== series_report('jev-system-one-deep-dive') ==")
        r = (await c.call_tool("series_report", {"series_id": "jev-system-one-deep-dive"})).structured_content
        for p in r["parts"]:
            print(f"part {p['part']}: {p['views']:>3} views  retention {p['retention_vs_part1']:.0%}  {p['title']}")
        print("biggest drop:", r["biggest_drop"])


if not SNAP.exists():
    sys.exit("no data/snapshot.db: this demo needs the author's sanitised snapshot (see blogmcp/snapshot.py)")
anyio.run(main)
