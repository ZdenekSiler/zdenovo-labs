"""An MCP server as an OAuth 2.1 resource server."""
from pydantic import AnyHttpUrl
from mcp.server import MCPServer
from mcp.server.auth.settings import AuthSettings
from mcp.server.auth.provider import TokenVerifier, AccessToken
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.mcpserver.exceptions import ToolError

# Toy token table. Real servers introspect (RFC 7662) or verify a JWT signature.
TOKENS = {
    "tok-reader": AccessToken(token="tok-reader", client_id="cli-1",
                              scopes=["workshop:read"], subject="user-42",
                              resource="https://mcp.example.com/mcp"),
    "tok-writer": AccessToken(token="tok-writer", client_id="cli-2",
                              scopes=["workshop:read", "workshop:write"], subject="user-7",
                              resource="https://mcp.example.com/mcp"),
}


class TableVerifier(TokenVerifier):
    async def verify_token(self, token: str) -> AccessToken | None:
        return TOKENS.get(token)


mcp = MCPServer(
    "workshop",
    version="0.10.0",
    token_verifier=TableVerifier(),
    auth=AuthSettings(
        issuer_url=AnyHttpUrl("https://auth.example.com"),
        resource_server_url=AnyHttpUrl("http://127.0.0.1:8938/mcp"),
        required_scopes=["workshop:read"],
    ),
)


@mcp.tool()
def whoami() -> str:
    """Report the authenticated principal and granted scopes."""
    tok = get_access_token()
    return f"subject={tok.subject} client_id={tok.client_id} scopes={','.join(tok.scopes)}"


@mcp.tool()
def delete_asset(name: str) -> str:
    """Permanently delete an asset. Requires the workshop:write scope."""
    tok = get_access_token()
    if "workshop:write" not in tok.scopes:
        raise ToolError("this token lacks the workshop:write scope; ask for elevated access")
    return f"deleted {name}"


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="127.0.0.1", port=8938)
