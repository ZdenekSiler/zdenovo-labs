from mcp.server import MCPServer
from mcp.server.mcpserver import Context

mcp = MCPServer("workshop", version="0.5.0")


@mcp.tool()
async def publish_preset(name: str, ctx: Context) -> str:
    """Publish a material preset as a new tool, then tell subscribers the list changed."""

    @mcp.tool(name=f"apply_{name}", description=f"Apply the {name} material preset.")
    def _apply(mesh: str) -> str:
        return f"applied {name} to {mesh}"

    await ctx.notify_tools_changed()
    return f"published apply_{name}"


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="127.0.0.1", port=8934)
