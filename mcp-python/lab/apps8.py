from pydantic import BaseModel
from mcp.server import MCPServer
from mcp.server.apps import Apps, client_supports_apps
from mcp.server.mcpserver import Context

apps = Apps()

HTML = """<!doctype html><meta charset=utf-8>
<style>body{font:14px system-ui;margin:0;padding:12px}
.bar{height:10px;background:#eee;border-radius:5px;overflow:hidden}
.fill{height:100%;background:#4ade80}</style>
<h3 id=t>budget</h3><div class=bar><div class=fill id=f></div></div><p id=n></p>
<script>
window.addEventListener('message', (e) => {
  const d = e.data?.toolResult?.structuredContent; if (!d) return;
  document.getElementById('t').textContent = d.asset;
  document.getElementById('f').style.width = Math.min(100, d.percent) + '%';
  document.getElementById('n').textContent = d.triangles + ' / ' + d.cap + ' triangles';
});
</script>"""

apps.add_html_resource("ui://workshop/budget", HTML, title="Polygon budget",
                       prefers_border=True)


class BudgetView(BaseModel):
    asset: str
    triangles: int
    cap: int
    percent: float


@apps.tool(resource_uri="ui://workshop/budget", title="Show polygon budget")
def show_budget(asset: str, triangles: int, ctx: Context) -> BudgetView:
    """Show an asset's polygon budget usage as a meter."""
    cap = 30_000
    return BudgetView(asset=asset, triangles=triangles, cap=cap,
                      percent=round(triangles / cap * 100, 1))


mcp = MCPServer("workshop", version="0.8.0", extensions=[apps])
