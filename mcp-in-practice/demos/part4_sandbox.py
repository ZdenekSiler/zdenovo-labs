"""Part 4: the same probe server on the host and in the sandbox, then the real server sandboxed."""

import shutil
import sys
import tempfile
import time
from pathlib import Path

import anyio
from mcp import Client, StdioServerParameters

LAB = Path(__file__).resolve().parents[1]
FIXTURE = LAB / "data" / "fixture.db"


async def probe(label: str, params: StdioServerParameters) -> dict[str, tuple[bool, str]]:
    async with Client(params) as c:
        r = await c.call_tool("try_everything", {})
    rows = r.structured_content["result"]
    return {p["attempt"]: (p["allowed"], p["detail"]) for p in rows}


async def main() -> None:
    scratch = Path(tempfile.mkdtemp())
    host_db = scratch / "blog.db"
    shutil.copy(FIXTURE, host_db)  # the host run gets a throwaway copy to "write" to
    host = await probe("host", StdioServerParameters(
        command=sys.executable, args=[str(LAB / "sandbox" / "naughty_server.py"), str(host_db)]))
    box = await probe("sandbox", StdioServerParameters(
        command=str(LAB / "sandbox" / "run.sh"), args=[str(FIXTURE), "naughty"]))
    shutil.rmtree(scratch)

    print("== 1. the probe server, on the host vs in the sandbox ==")
    print(f"{'attempt':30} {'host':6} {'sandbox':8} sandbox detail")
    for attempt, (ok_host, _) in host.items():
        ok_box, detail = box[attempt]
        print(f"{attempt:30} {'yes' if ok_host else 'no':6} {'yes' if ok_box else 'no':8} {detail}")
    print("\n-- what the host run could do --")
    for attempt in ("list your home directory", "see secret-looking env vars", "write to the blog database"):
        print(f"{attempt:30} {host[attempt][1]}")

    print("\n== 2. the real server through the sandbox ==")
    t0 = time.perf_counter()
    async with Client(StdioServerParameters(command=str(LAB / "sandbox" / "run.sh"), args=[str(FIXTURE)])) as c:
        startup = time.perf_counter() - t0
        tools = [t.name for t in (await c.list_tools()).tools]
        r = await c.call_tool("top_articles", {"limit": 3})
        proto = c.protocol_version
    print(f"protocol {proto}, connected in {startup:.1f}s, tools: {tools}")
    for a in r.structured_content["articles"]:
        print(f"  {a['rank']}. {a['title']} ({a['views']} views)")


anyio.run(main)
