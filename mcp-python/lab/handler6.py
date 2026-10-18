import logging, threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
import anyio
from mcp.server import MCPServer
from mcp.server.mcpserver import Context

log = logging.getLogger("workshop")


@dataclass
class Deps:
    pool: str
    started: float


@asynccontextmanager
async def lifespan(server: MCPServer) -> AsyncIterator[Deps]:
    log.warning("lifespan: opening pool")
    deps = Deps(pool="pg://fake", started=anyio.current_time())
    try:
        yield deps
    finally:
        log.warning("lifespan: closing pool")


mcp = MCPServer("workshop", version="0.6.0", lifespan=lifespan)


@mcp.tool()
def where_am_i(ctx: Context) -> str:
    """Report the thread and request id this handler runs on."""
    deps = ctx.request_context.lifespan_context
    return (f"thread={threading.current_thread().name} request_id={ctx.request_id} "
            f"pool={deps.pool} headers={'yes' if ctx.headers else 'none'}")


@mcp.tool()
async def where_am_i_async(ctx: Context) -> str:
    """Same, but async."""
    return f"thread={threading.current_thread().name} request_id={ctx.request_id}"


@mcp.tool()
async def long_bake(seconds: int, ctx: Context) -> str:
    """Pretend to bake for N seconds, reporting progress; logs if cancelled."""
    try:
        for i in range(seconds):
            await ctx.report_progress(i + 1, seconds, f"second {i+1}")
            log.warning("bake: step %d/%d", i + 1, seconds)
            await anyio.sleep(1)
        return "baked"
    except anyio.get_cancelled_exc_class():
        log.warning("bake: CANCELLED at step %d", i + 1)
        raise


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    mcp.run(transport="streamable-http", host="127.0.0.1", port=8935)
