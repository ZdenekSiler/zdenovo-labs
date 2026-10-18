import anyio
from mcp.server import MCPServer
from mcp.server.mcpserver import Context

mcp = MCPServer("workshop", version="0.2.0")


@mcp.tool()
async def bake_lightmap(scene: str, passes: int = 3, ctx: Context = None) -> str:
    """Bake a lightmap for a scene. Slow on purpose."""
    for i in range(passes):
        await ctx.report_progress(i + 1, passes, f"pass {i + 1}/{passes}")
        await anyio.sleep(0.4)
    return f"Baked {scene} in {passes} passes."


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="127.0.0.1", port=8932)
