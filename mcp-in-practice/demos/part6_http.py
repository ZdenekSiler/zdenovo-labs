"""Part 6: the same server over Streamable HTTP, and the Host check you'll meet in production."""
import subprocess
import sys
import time
from pathlib import Path

import anyio
import httpx2
from mcp import Client

LAB = Path(__file__).resolve().parents[1]
PORT = 8765
URL = f"http://127.0.0.1:{PORT}/mcp"


async def main() -> None:
    proc = subprocess.Popen([sys.executable, "-m", "blogmcp.server", str(LAB / "data" / "fixture.db"),
                             "--http", str(PORT)], cwd=LAB, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            try:
                httpx2.get(f"http://127.0.0.1:{PORT}/", timeout=0.2)
                break
            except httpx2.TransportError:
                time.sleep(0.1)
        async with Client(URL) as c:
            r = await c.call_tool("top_articles", {"limit": 2})
            print(f"== 1. over HTTP: protocol {c.protocol_version} ==")
            for a in r.structured_content["articles"]:
                print(f"  {a['rank']}. {a['title']} ({a['views']} views)")
        print("\n== 2. the same request, arriving with a public Host header ==")
        # A well-formed 2026-07-28 request (see deep-dive part 2): version header + Mcp-Method + _meta.
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientInfo": {"name": "demo", "version": "1"},
            "io.modelcontextprotocol/clientCapabilities": {}}}}
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
                   "MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "tools/list"}
        for host in (f"127.0.0.1:{PORT}", "blog-mcp.example.com"):
            resp = httpx2.post(URL, json=body, headers={**headers, "Host": host})
            print(f"Host: {host:24} -> HTTP {resp.status_code} {resp.text.strip()[:90]}")
    finally:
        proc.terminate()
        proc.wait()


anyio.run(main)
