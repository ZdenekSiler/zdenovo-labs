"""workshop — the MCP server for the game side of the loop.

Tools:
  validate_glb   parse a .glb and report triangles, materials, nodes against a budget
  list_assets    what's in the assets directory
  serve_game     start the static server (idempotent) and return its URL
  screenshot     render the running game headlessly and return a PNG + a link
  playtest       load the game, read its HUD, and report what the browser actually did
"""
from __future__ import annotations

import json
import re
import struct
import subprocess
import time
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field
from mcp.server import MCPServer
from mcp.server.caching import CacheHint
from mcp.server.mcpserver import Context, Image
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ResourceLink

GAME_DIR = Path(__file__).parent
ASSETS = GAME_DIR / "assets"
SHOTS = GAME_DIR / "shots"
CHROME = "google-chrome"
BUDGETS = {"mobile": 30_000, "desktop": 250_000, "vr": 90_000}

mcp = MCPServer(
    "workshop",
    version="1.0.0",
    instructions=(
        "Tools for building a Three.js browser game from exported glTF assets. "
        "Typical loop: validate_glb -> serve_game -> playtest -> screenshot."
    ),
    cache_hints={"tools/list": CacheHint(ttl_ms=300_000, scope="public")},
)

_server_proc: subprocess.Popen | None = None
_server_port: int | None = None


# ---------------------------------------------------------------- validate

class GlbReport(BaseModel):
    """What a .glb contains, measured rather than guessed."""

    path: str
    bytes_: int = Field(description="File size in bytes.")
    triangles: int
    meshes: int
    materials: int
    nodes: list[str] = Field(description="Node names, which gameplay code looks up.")
    nodes_at_origin: list[str] = Field(
        description="Nodes with no translation. Several of these usually means the "
        "exporter baked offsets into the vertices, which breaks position-based gameplay."
    )
    budget: str
    triangle_cap: int
    passes: bool
    warnings: list[str] = Field(default_factory=list)


def _read_glb(path: Path) -> tuple[dict, int]:
    raw = path.read_bytes()
    if raw[:4] != b"glTF":
        raise ToolError(f"{path.name} is not a binary glTF (magic is {raw[:4]!r}); "
                        "export with format='glb'")
    version, _total = struct.unpack_from("<II", raw, 4)
    if version != 2:
        raise ToolError(f"unsupported glTF container version {version}; expected 2")
    length, kind = struct.unpack_from("<I", raw, 12)[0], raw[16:20]
    if kind != b"JSON":
        raise ToolError("first chunk is not JSON; the file looks truncated")
    return json.loads(raw[20:20 + length]), len(raw)


@mcp.tool(title="Validate a .glb against the project's budget")
def validate_glb(
    filename: Annotated[str, Field(description="File name inside the assets directory, e.g. 'arena.glb'.")],
    target: Annotated[Literal["mobile", "desktop", "vr"], Field(description="Platform budget to check against.")] = "mobile",
) -> GlbReport:
    """Parse an exported .glb and report triangles, materials and node layout against
    the platform's budget. Run this after every export from Blender and before rebuilding
    the game; a file that fails here will load but perform or behave badly in the browser."""
    path = ASSETS / filename
    if not path.exists():
        have = ", ".join(sorted(p.name for p in ASSETS.glob("*.glb"))) or "nothing"
        raise ToolError(f"no such asset {filename!r}; assets/ currently holds: {have}")

    gltf, size = _read_glb(path)
    accessors = gltf.get("accessors", [])
    triangles = 0
    for mesh in gltf.get("meshes", []):
        for prim in mesh.get("primitives", []):
            if prim.get("mode", 4) != 4:
                continue
            if "indices" in prim:
                triangles += accessors[prim["indices"]]["count"] // 3
            else:
                triangles += accessors[prim["attributes"]["POSITION"]]["count"] // 3

    nodes = gltf.get("nodes", [])
    names = [n.get("name", f"node{i}") for i, n in enumerate(nodes)]
    at_origin = [n.get("name", f"node{i}") for i, n in enumerate(nodes)
                 if not n.get("translation") and not n.get("matrix")]

    cap = BUDGETS[target]
    warnings: list[str] = []
    if len(at_origin) > 1:
        warnings.append(
            f"{len(at_origin)} nodes have no translation ({', '.join(at_origin[:4])}"
            f"{'...' if len(at_origin) > 4 else ''}); gameplay that reads node positions "
            "will treat them as all being at the same place")
    if any(n.get("camera") is not None for n in nodes):
        warnings.append("the export contains a camera; the game supplies its own")
    if any(name.lower().startswith(("light", "lamp", "sun")) for name in names):
        warnings.append("the export appears to contain a light; it will add to the game's lighting")
    if size > 5_000_000:
        warnings.append(f"{size / 1e6:.1f} MB is slow to fetch on a phone; check texture sizes")

    return GlbReport(
        path=str(path), bytes_=size, triangles=triangles,
        meshes=len(gltf.get("meshes", [])), materials=len(gltf.get("materials", [])),
        nodes=names, nodes_at_origin=at_origin, budget=target, triangle_cap=cap,
        passes=triangles <= cap and not at_origin[1:], warnings=warnings,
    )


@mcp.tool()
def list_assets() -> list[str]:
    """List the asset files the game can load."""
    return sorted(p.name for p in ASSETS.iterdir() if p.is_file())


# ---------------------------------------------------------------- serve

@mcp.tool(title="Serve the game")
def serve_game(port: Annotated[int, Field(ge=1024, le=65535)] = 8099) -> str:
    """Start the game's static web server if it isn't already running, and return its URL.
    Safe to call repeatedly; it will not start a second server."""
    global _server_proc, _server_port
    if _server_proc and _server_proc.poll() is None:
        return f"already serving on http://127.0.0.1:{_server_port}/"
    _server_proc = subprocess.Popen(
        ["python3", "-m", "http.server", str(port), "--bind", "127.0.0.1",
         "--directory", str(GAME_DIR)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    _server_port = port
    time.sleep(1.0)
    if _server_proc.poll() is not None:
        raise ToolError(f"the static server exited immediately; is port {port} already in use?")
    return f"serving http://127.0.0.1:{port}/"


def _chrome(*args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    # --enable-unsafe-swiftshader is required: headless Chrome has no GPU, and without a
    # software rasteriser WebGLRenderer throws "Error creating WebGL context" and the
    # page renders as an empty background.
    return subprocess.run(
        [CHROME, "--headless", "--no-sandbox", "--enable-unsafe-swiftshader",
         "--hide-scrollbars", *args],
        capture_output=True, text=True, timeout=timeout,
    )


# ---------------------------------------------------------------- look

@mcp.tool(title="Screenshot the running game")
async def screenshot(
    name: Annotated[str, Field(description="Name for the screenshot file, without extension.")] = "frame",
    width: Annotated[int, Field(ge=320, le=3840)] = 1200,
    height: Annotated[int, Field(ge=240, le=2160)] = 750,
    settle_ms: Annotated[int, Field(ge=500, le=30_000, description="How long to let the scene load and animate before capturing.")] = 9000,
    ctx: Context = None,
) -> ResourceLink:
    """Render the running game in a headless browser and save a PNG. Call serve_game first.
    Returns a link to the file rather than the image bytes, so the screenshot doesn't
    consume the conversation's context."""
    if not _server_port:
        raise ToolError("the game isn't being served; call serve_game first")
    SHOTS.mkdir(exist_ok=True)
    out = SHOTS / f"{name}.png"
    await ctx.report_progress(1, 2, "rendering")
    proc = _chrome(f"--virtual-time-budget={settle_ms}", f"--window-size={width},{height}",
                   f"--screenshot={out}", f"http://127.0.0.1:{_server_port}/")
    if not out.exists():
        raise ToolError(f"chrome produced no screenshot: {proc.stderr.strip()[-300:]}")
    await ctx.report_progress(2, 2, "captured")
    return ResourceLink(
        type="resource_link", uri=out.as_uri(), name=out.name, mimeType="image/png",
        description=f"{width}x{height} render of the game after {settle_ms} ms",
    )


class Playtest(BaseModel):
    """What the browser actually did with the game."""

    loaded: bool
    crates_found: int
    crates_collected: int
    elapsed_label: str
    message: str = Field(description="On-screen message, empty if none.")
    verdict: str


@mcp.tool(title="Playtest the game in a browser")
def playtest(settle_ms: Annotated[int, Field(ge=500, le=30_000)] = 9000) -> Playtest:
    """Load the running game in a headless browser and read its HUD back, so you can tell
    whether the asset actually loaded and the scene wired up - without looking at a picture.
    Call serve_game first. Use this after every asset change; a game that renders a nice
    screenshot can still have found zero objects."""
    if not _server_port:
        raise ToolError("the game isn't being served; call serve_game first")
    proc = _chrome(f"--virtual-time-budget={settle_ms}", "--dump-dom",
                   f"http://127.0.0.1:{_server_port}/")
    dom = proc.stdout

    def grab(pattern: str, default: str = "") -> str:
        m = re.search(pattern, dom)
        return m.group(1) if m else default

    total = int(grab(r'id="total">(\d+)', "0"))
    score = int(grab(r'id="score">(\d+)', "0"))
    clock = grab(r'id="clock">([\d.]+)', "0.0")
    message = grab(r'id="msg">([^<]*)').strip()

    if total == 0:
        verdict = ("no objects found - the .glb did not load, or its node names don't match "
                   "what the game looks for (Player, Crate*)")
    elif score == total and clock in ("0.0", "0.1"):
        verdict = ("every crate was collected instantly - the crate nodes are probably all at "
                   "the origin, so the distance check fires on frame one")
    else:
        verdict = f"scene loaded: {total} crates, player present, clock running at {clock}s"

    return Playtest(loaded=total > 0, crates_found=total, crates_collected=score,
                    elapsed_label=f"{clock}s", message=message, verdict=verdict)


if __name__ == "__main__":
    mcp.run()
