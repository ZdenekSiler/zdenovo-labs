from mcp.server import MCPServer
mcp = MCPServer("workshop")

@mcp.tool()
def budget(triangles, target="mobile"):
    """Check a budget."""
    return "ok"
