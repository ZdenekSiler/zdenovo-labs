"""The smallest useful MCP server: one tool over the blog database."""
import sqlite3
import sys

from mcp.server import MCPServer

DB = sys.argv[1] if len(sys.argv) > 1 else "data/fixture.db"
mcp = MCPServer("hello-blog")


@mcp.tool()
def most_read(limit: int = 3) -> list[dict]:
    """The most viewed articles on the blog, most viewed first."""
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = db.execute("SELECT title, views FROM posts ORDER BY views DESC LIMIT ?", (limit,))
    return [{"title": t, "views": v} for t, v in rows]


if __name__ == "__main__":
    mcp.run()
