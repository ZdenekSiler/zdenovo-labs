from typing import Annotated, Literal
from pydantic import BaseModel, Field
from mcp.server import MCPServer
from mcp.server.mcpserver import Context, Elicit, Resolve
from mcp.server.mcpserver import AcceptedElicitation

mcp = MCPServer("workshop", version="0.7.0")


class ExportOptions(BaseModel):
    """How to export the scene."""
    fmt: Literal["glb", "gltf"] = Field(description="Binary .glb or text .gltf")
    draco: bool = Field(description="Compress meshes with Draco?")


@mcp.tool()
async def export_scene(name: str, ctx: Context) -> str:
    """Export a scene, asking the user for format options first."""
    result = await ctx.elicit(f"How should {name!r} be exported?", ExportOptions)
    if result.action != "accept":
        return f"Export cancelled by user (action={result.action})."
    opts = result.data
    return f"Exported {name}.{opts.fmt} (draco={opts.draco})."


async def pick_target(ctx: Context) -> "Elicit[Target] | Target":
    """Resolver: use the header if the host set one, otherwise ask."""
    hinted = (ctx.headers or {}).get("x-target-platform")
    if hinted:
        return Target(platform=hinted)
    return Elicit("Which platform is this build for?", Target)


class Target(BaseModel):
    platform: Literal["mobile", "desktop", "vr"] = Field(description="Target platform")


@mcp.tool()
async def build(scene: str, target: Annotated[Target, Resolve(pick_target)]) -> str:
    """Build a scene for a platform, asking only if the platform isn't already known."""
    return f"Built {scene} for {target.platform}."


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="127.0.0.1", port=8936)
