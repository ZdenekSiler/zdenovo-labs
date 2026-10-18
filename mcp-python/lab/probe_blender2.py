import anyio, json
from mcp import Client, StdioServerParameters

async def main():
    params = StdioServerParameters(command="uvx", args=["mcp-for-blender"])
    async with Client(params) as c:
        t = {x.name: x for x in (await c.list_tools()).tools}
        print("=== export_scene schema ===")
        print(json.dumps(t["export_scene"].input_schema, indent=1)[:1400])
        print("\n=== export_scene description ===")
        print(t["export_scene"].description)
        print("\n=== call with no Blender running ===")
        r = await c.call_tool("get_scene_info", {"user_prompt": "what is in the scene?"})
        print("is_error:", r.is_error)
        print("text:", r.content[0].text[:400])
anyio.run(main)
