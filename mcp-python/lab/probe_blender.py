import anyio, json
from mcp import Client, StdioServerParameters

async def main():
    params = StdioServerParameters(command="uvx", args=["mcp-for-blender"])
    async with Client(params) as c:
        print("server_info:", c.server_info)
        print("protocol:", c.protocol_version)
        tools = await c.list_tools()
        print(f"\n{len(tools.tools)} tools:\n")
        for t in tools.tools:
            props = list((t.input_schema or {}).get("properties", {}))
            print(f"  {t.name}({', '.join(props)})")
            print(f"      {(t.description or '').strip().splitlines()[0][:110]}")

anyio.run(main)
