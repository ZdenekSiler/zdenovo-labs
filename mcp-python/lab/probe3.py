import anyio, json
from mcp import Client
from tools3 import mcp

async def main():
    async with Client(mcp) as c:
        t = (await c.list_tools()).tools[0]
        print("=== tool json ===")
        print(json.dumps(t.model_dump(exclude_none=True, by_alias=True), indent=2)[:2200])
        print("\n=== bad enum value ===")
        r = await c.call_tool("poly_budget", {"triangles": 1000, "target": "switch"})
        print("is_error:", r.is_error, "|", r.content[0].text[:300])
        print("\n=== wrong type ===")
        r = await c.call_tool("poly_budget", {"triangles": "lots"})
        print("is_error:", r.is_error, "|", r.content[0].text[:300])
        print("\n=== out of range ===")
        r = await c.call_tool("poly_budget", {"triangles": -5})
        print("is_error:", r.is_error, "|", r.content[0].text[:300])
        print("\n=== exception in handler ===")
        r = await c.call_tool("explode", {"path": "/tmp/x"})
        print("is_error:", r.is_error, "|", r.content[0].text[:300])
anyio.run(main)
