"""Client side of the agent's computer. Owns the runner process, its timeouts and its restarts."""
from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
import urllib.parse
import uuid
from pathlib import Path


class CommandRunner:
    def __init__(self, workspace: Path, pypi_base: str, slow_s: float = 0.0):
        self.workspace = workspace
        self.pypi_base = pypi_base
        self.slow_s = slow_s
        self.proc: subprocess.Popen | None = None
        self._out: queue.Queue = queue.Queue()
        self.restarts = 0

    def start(self) -> None:
        host = urllib.parse.urlsplit(self.pypi_base).netloc
        cmd = [sys.executable, "-m", "lrlab.runner_server", "--workspace", str(self.workspace),
               "--pypi-base", self.pypi_base, "--allow-host", host, "--slow-s", str(self.slow_s)]
        # The computer gets a scrubbed environment: no API keys, no DB path, no tokens.
        env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": str(Path(__file__).resolve().parent.parent)}
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=env)
        self._out = queue.Queue()
        threading.Thread(target=self._reader, args=(self.proc, self._out), daemon=True).start()

    @staticmethod
    def _reader(proc: subprocess.Popen, out: queue.Queue) -> None:
        for line in proc.stdout:
            out.put(json.loads(line))
        out.put(None)

    def call(self, tool: str, args: dict, timeout_s: float = 30.0, scope: str | None = None) -> dict:
        if self.proc is None or self.proc.poll() is not None:
            self.start()
        rid = uuid.uuid4().hex[:8]
        self.proc.stdin.write(json.dumps({"id": rid, "tool": tool, "args": args, "scope": scope}) + "\n")
        self.proc.stdin.flush()
        try:
            resp = self._out.get(timeout=timeout_s)
        except queue.Empty:
            self.kill()                                   # a hung tool costs one process, not the run
            self.restarts += 1
            return {"ok": False, "transient": True, "error": f"tool timed out after {timeout_s}s; computer restarted"}
        if resp is None:
            self.restarts += 1
            self.proc = None
            return {"ok": False, "transient": True, "error": "computer process died; restarted"}
        return resp

    def kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        self.proc = None

    close = kill
