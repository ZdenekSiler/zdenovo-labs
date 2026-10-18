from typing import Annotated
from pydantic import Field
from mcp.server import MCPServer
from mcp.server.mcpserver.prompts.base import UserMessage, AssistantMessage, Message

mcp = MCPServer("workshop", version="0.5.0")

CATALOG = {"hero": {"tris": 14200, "mats": 2}, "arena": {"tris": 184000, "mats": 9}}


@mcp.resource("config://asset-rules", mime_type="application/json")
def asset_rules() -> dict:
    """The project's asset budget rules."""
    print("!! handler ran !!")
    return {"mobile_tris": 30000, "max_texture": 2048, "formats": ["glb"]}


@mcp.resource("asset://{name}/manifest", mime_type="application/json")
def asset_manifest(name: str) -> dict:
    """Manifest for one asset in the catalog."""
    if name not in CATALOG:
        raise ValueError(f"unknown asset {name!r}")
    return {"name": name, **CATALOG[name]}


@mcp.resource("file://project/{+path}")
def project_file(path: str) -> str:
    """Read a file from the project tree."""
    return f"contents of {path}"


@mcp.prompt(title="Asset review")
def review_asset(
    name: Annotated[str, Field(description="Asset name from the catalog.")],
    strictness: Annotated[str, Field(description="loose | normal | strict")] = "normal",
) -> list[Message]:
    """Review an exported asset against the project rules."""
    return [
        UserMessage(f"Review the asset {name!r} at {strictness} strictness."),
        UserMessage("Rules are in config://asset-rules. Manifest is at asset://" + name + "/manifest."),
        AssistantMessage("I'll read the rules and the manifest first, then report violations in order of severity."),
    ]
