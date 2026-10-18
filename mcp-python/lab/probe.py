import anyio, json
from mcp import Client
from hello import mcp


async def main() -> None:
    async with Client(mcp) as client:
        print("protocol_version:", client.protocol_version)
        print("server_info:", client.server_info)
        print("capabilities:", client.server_capabilities.model_dump(exclude_none=True))
        tools = await client.list_tools()
        for t in tools.tools:
            print("\ntool:", t.name)
            print("description:", t.description)
            print("input_schema:", json.dumps(t.input_schema, indent=2))
            print("output_schema:", json.dumps(t.output_schema, indent=2) if t.output_schema else None)
        res = await client.call_tool("poly_budget", {"triangles": 48000})
        print("\ncontent:", res.content)
        print("structured_content:", res.structured_content)
        print("is_error:", res.is_error)


anyio.run(main)
