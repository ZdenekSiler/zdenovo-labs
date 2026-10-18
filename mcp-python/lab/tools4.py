from dataclasses import dataclass
from pydantic import BaseModel, Field
from mcp.server import MCPServer
from mcp.server.mcpserver import Image
from mcp.types import ResourceLink

mcp = MCPServer("workshop", version="0.4.0")


class AssetReport(BaseModel):
    """A validation report for one exported asset."""
    asset: str
    triangles: int = Field(description="Triangle count after export.")
    materials: int
    warnings: list[str] = Field(default_factory=list)
    passes: bool


@mcp.tool()
def validate_asset(path: str) -> AssetReport:
    """Validate an exported .glb against the project's asset rules."""
    return AssetReport(asset=path, triangles=18422, materials=2,
                       warnings=["texture 'brick_d.png' is 4096px; budget is 2048px"], passes=True)


@dataclass
class Slot:
    name: str
    bytes_: int


@mcp.tool()
def list_slots() -> list[Slot]:
    """List asset slots in the current level."""
    return [Slot("hero", 412_000), Slot("arena", 3_900_000)]


@mcp.tool(structured_output=False)
def describe_scene(name: str) -> str:
    """Describe a scene in prose for the model."""
    return f"{name}: 3 meshes, 2 lights, 1 camera."


@mcp.tool()
def thumbnail(asset: str) -> Image:
    """Render a small PNG thumbnail of an asset."""
    png = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
                        "0000000a49444154789c6360000002000100ffff03000006000557bfabd4"
                        "0000000049454e44ae426082")
    return Image(data=png, format="png")


@mcp.tool()
def export_scene(name: str) -> ResourceLink:
    """Export a scene and return a link to the resulting file."""
    return ResourceLink(type="resource_link", uri=f"file:///assets/{name}.glb",
                        name=f"{name}.glb", mimeType="model/gltf-binary",
                        description="Exported scene, binary glTF")
