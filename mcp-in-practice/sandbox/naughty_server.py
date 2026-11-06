"""An MCP server that tries what a malicious or compromised one would, and reports the result.

Safe to run on your own machine: it lists names (never reads file contents or secret values),
its writes go to throwaway paths, and its database "write" takes a write lock and rolls back.
Run it outside and inside the sandbox and compare (demos/part4_sandbox.py).
"""

from __future__ import annotations

import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from pydantic import BaseModel

from mcp.server import MCPServer


class Probe(BaseModel):
    attempt: str
    allowed: bool
    detail: str


def _try(attempt: str, fn) -> Probe:
    try:
        return Probe(attempt=attempt, allowed=True, detail=str(fn())[:120])
    except Exception as e:  # noqa: BLE001 - the failure IS the result
        return Probe(attempt=attempt, allowed=False, detail=f"{type(e).__name__}: {e}"[:120])


def probes(db: str) -> list[Probe]:
    home = Path.home()
    app = Path(__file__).resolve().parent

    def write_app():
        p = app / "pwned.txt"
        p.write_text("x")
        p.unlink()
        return f"wrote and removed {p}"

    def write_db():
        # Read-write mode, ignoring the server's read-only opener. A real UPDATE, rolled back:
        # taking a lock is NOT proof of a write (SQLite grants locks on read-only files; the
        # first version of this probe did that and reported a write that couldn't happen).
        c = sqlite3.connect(db)
        try:
            n = c.execute("UPDATE posts SET views = views WHERE rowid = (SELECT min(rowid) FROM posts)").rowcount
            return f"updated {n} row (then rolled back)"
        finally:
            c.rollback()
            c.close()

    def home_listing():
        names = os.listdir(home)
        ssh = (home / ".ssh").exists()
        return f"{home}: {len(names)} entries; ~/.ssh exists={ssh}"

    def secret_env():
        names = sorted(k for k in os.environ if any(w in k.upper() for w in ("KEY", "TOKEN", "SECRET", "PASS")))
        if not names:
            raise PermissionError("no secret-looking variables visible")
        return f"{len(names)} visible: {', '.join(names[:4])}{' …' if len(names) > 4 else ''}"

    def outbound():
        with socket.create_connection(("1.1.1.1", 443), timeout=3):
            return "TCP connect to 1.1.1.1:443 succeeded"

    def dns():
        return f"example.com -> {socket.gethostbyname('example.com')}"

    def spawn():
        procs, err = [], None
        try:
            for _ in range(100):
                procs.append(subprocess.Popen(["sleep", "2"]))
        except OSError as e:
            err = e
        n = len(procs)
        for p in procs:
            p.kill()
            p.wait()
        if err:
            raise OSError(f"started {n}/100, then: {err}")
        return f"started {n}/100 processes"

    def big_tmp():
        with tempfile.NamedTemporaryFile(dir="/tmp") as f:
            f.write(b"\0" * (32 * 1024 * 1024))
            f.flush()
            return "wrote 32 MiB to /tmp"

    return [
        _try("identity", lambda: f"uid={os.getuid()} gid={os.getgid()}"),
        _try("write next to its own code", write_app),
        _try("write to the blog database", write_db),
        _try("list your home directory", home_listing),
        _try("see secret-looking env vars", secret_env),
        _try("open an outbound connection", outbound),
        _try("resolve DNS", dns),
        _try("spawn 100 processes", spawn),
        _try("fill /tmp with 32 MiB", big_tmp),
    ]


def build(db: str) -> MCPServer:
    mcp = MCPServer("naughty")

    @mcp.tool()
    def try_everything() -> list[Probe]:
        """Attempt the things a compromised MCP server would, and report each outcome."""
        return probes(db)

    return mcp


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else os.environ.get("BLOG_DB", "/data/blog.db")).run()
