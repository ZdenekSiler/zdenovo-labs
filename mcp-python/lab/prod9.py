"""MCP mounted inside an existing FastAPI/Starlette app, production-shaped."""
import os
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

mcp = MCPServer("workshop", version="0.9.0")


@mcp.tool()
def poly_budget(triangles: int) -> str:
    """Judge a triangle count against the mobile budget."""
    return f"{triangles:,} triangles {'fits' if triangles <= 30_000 else 'over budget'}."


security = TransportSecuritySettings(
    allowed_hosts=["mcp.example.com", "mcp.example.com:*", "127.0.0.1:*"],
    allowed_origins=["https://app.example.com"],
)

mcp_app = mcp.streamable_http_app(transport_security=security)


async def health(request):
    return JSONResponse({"status": "ok", "version": "0.9.0"})


app = Starlette(routes=[Route("/healthz", health), Mount("/", app=mcp_app)],
                lifespan=mcp_app.router.lifespan_context)
