from typing import Annotated, Literal
from pydantic import BaseModel, Field
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import ToolAnnotations

mcp = MCPServer("workshop", version="0.3.0")


@mcp.tool(
    title="Check polygon budget",
    annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False, idempotent_hint=True),
)
def poly_budget(
    triangles: Annotated[int, Field(ge=0, le=50_000_000, description="Triangle count of the mesh, not vertices.")],
    target: Annotated[Literal["mobile", "desktop", "vr"], Field(description="Platform the asset ships to.")] = "mobile",
) -> str:
    """Judge whether a mesh fits a platform's triangle budget. Use before exporting an asset."""
    caps = {"mobile": 30_000, "desktop": 250_000, "vr": 90_000}
    cap = caps[target]
    return f"{triangles:,} triangles {'fits' if triangles <= cap else 'over budget'} for {target} (cap {cap:,})."


@mcp.tool()
def explode(path: str) -> str:
    """Raise a plain exception."""
    raise RuntimeError(f"boom while reading {path}")


if __name__ == "__main__":
    mcp.run()
