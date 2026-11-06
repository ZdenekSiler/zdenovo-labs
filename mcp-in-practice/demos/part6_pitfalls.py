"""Part 6: the server returns untrusted text, because the blog writes about attacks."""
import json
from pathlib import Path

import anyio
from mcp import Client

from blogmcp.server import build_server

LAB = Path(__file__).resolve().parents[1]


async def main() -> None:
    async with Client(build_server(str(LAB / "data" / "fixture.db"))) as c:
        print("== 1. search returns the attack string, verbatim ==")
        r = await c.call_tool("search_articles", {"query": "ignore previous instructions"})
        for h in r.structured_content["hits"]:
            print(f"{h['slug']}: {h['snippet']!r}")
        print("\n== 2. the resource returns the whole article ==")
        text = (await c.read_resource("blog://posts/prompt-injection-is-not-a-bug")).contents[0].text
        print("\n".join(line for line in text.splitlines() if line.startswith(">")))
        print("\n== 3. what the server tells the host about it ==")
        print("instructions:", " ".join((c.instructions or "").split()))
        tool = next(t for t in (await c.list_tools()).tools if t.name == "search_articles")
        print("search_articles:", " ".join(tool.description.split()))
        print("snippet field :", tool.output_schema["$defs"]["SearchHit"]["properties"]["snippet"]["description"])


anyio.run(main)
