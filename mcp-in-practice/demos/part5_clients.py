"""Part 5: what a host turns MCP tools into, and how to register the sandboxed server."""
import json
from pathlib import Path

import anyio
from mcp import Client

from blogmcp.server import build_server

LAB = Path(__file__).resolve().parents[1]


async def main() -> None:
    async with Client(build_server(str(LAB / "data" / "fixture.db"))) as c:
        tool = next(t for t in (await c.list_tools()).tools if t.name == "series_report")
    print("== 1. one MCP tool, as the host hands it to Claude ==")
    print(json.dumps({"name": tool.name, "description": tool.description,
                      "input_schema": tool.input_schema}, indent=2))
    print("\n== 2. registering the sandboxed server with Claude Code ==")
    print('cd lab && claude mcp add blog -- "$PWD/sandbox/run.sh" "$PWD/data/fixture.db"')
    print("\n== 3. the same thing as a project .mcp.json ==")
    print(json.dumps({"mcpServers": {"blog": {"command": "/path/to/lab/sandbox/run.sh",
                                               "args": ["/path/to/lab/data/fixture.db"]}}}, indent=2))


anyio.run(main)
