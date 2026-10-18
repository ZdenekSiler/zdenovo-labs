from mcp.server import MCPServer

mcp = MCPServer("workshop")


@mcp.tool()
def poly_budget(triangles: int, target: str = "mobile") -> str:
    """Judge whether a mesh's triangle count fits a platform budget."""
    budgets = {"mobile": 30_000, "desktop": 250_000}
    cap = budgets.get(target, 30_000)
    verdict = "fits" if triangles <= cap else "over budget"
    return f"{triangles:,} triangles {verdict} for {target} (cap {cap:,})."


if __name__ == "__main__":
    mcp.run()
