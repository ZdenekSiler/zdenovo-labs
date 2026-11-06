"""Part 1: what a client sees when it talks to the 15-line server."""
import json
import sys
from pathlib import Path

import anyio
from mcp import Client, StdioServerParameters

LAB = Path(__file__).resolve().parents[1]


async def main() -> None:
    server = StdioServerParameters(command=sys.executable,
                                   args=[str(LAB / "demos" / "hello_blog.py"), str(LAB / "data" / "fixture.db")])
    async with Client(server) as c:  # spawns the server as a subprocess, talks JSON-RPC over stdio
        print("protocol:", c.protocol_version)
        print("server:", c.server_info.name)
        tool = (await c.list_tools()).tools[0]
        print("\n== what the model is shown ==")
        print(json.dumps({"name": tool.name, "description": tool.description,
                          "inputSchema": tool.input_schema, "outputSchema": tool.output_schema}, indent=2))
        r = await c.call_tool("most_read", {"limit": 3})
        print("\n== tools/call most_read {limit: 3} ==")
        print(f"content: {len(r.content)} text blocks, one per list item; first = {r.content[0].text!r}")
        print("structuredContent:", json.dumps(r.structured_content))


anyio.run(main)
