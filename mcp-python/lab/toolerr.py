from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

mcp = MCPServer("workshop")

@mcp.tool()
def export_scene(path: str) -> str:
    """Export the current scene to a .glb file."""
    if not path.endswith(".glb"):
        raise ToolError(f"path must end in .glb (got {path!r}); try 'assets/arena.glb'")
    return f"exported {path}"
