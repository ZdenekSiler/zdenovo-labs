"""The agent's computer: a separate process that actually touches files and the network.

Run as `python -m lrlab.runner_server --workspace DIR --pypi-base URL --allow-host HOST`.
Protocol: one JSON request per line on stdin, one JSON response per line on stdout.

Why a separate process (and in production, a separate container or VM):
  * the harness process holds API keys and the database; this one gets neither (see runner.py: env is scrubbed)
  * a hung tool can be killed without killing the conversation
  * the blast radius of a bad tool call is the workspace directory

This module is deliberately boring: no model, no state store, no knowledge of conversations.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import xml.etree.ElementTree as ET
from pathlib import Path

USER_AGENT = "lrlab-nightly-agent/1.0 (+https://github.com/ZdenekSiler/zdenovo-labs)"
ALLOWED_COMMANDS = {"ls", "wc", "grep", "head"}
STABLE = re.compile(r"^\d+(\.\d+)*$")


class Transient(Exception):
    """Network-shaped failure: worth retrying later, not worth showing the model."""


class Computer:
    def __init__(self, workspace: Path, pypi_base: str, allow_hosts: set[str], slow_s: float = 0.0):
        self.ws = workspace.resolve()
        self.pypi_base = pypi_base.rstrip("/")
        self.allow_hosts = allow_hosts
        self.slow_s = slow_s                      # chaos drills widen the crash window with this
        self.scope = "default"                    # cache scope = conversation (one night)
        self._robots: dict[str, urllib.robotparser.RobotFileParser] = {}

    # ── jail ─────────────────────────────────────────────────────────────────
    def _path(self, rel: str) -> Path:
        p = (self.ws / rel).resolve()
        if p != self.ws and self.ws not in p.parents:
            raise PermissionError(f"path escapes the workspace: {rel}")
        if ".cache" in p.relative_to(self.ws).parts:
            raise PermissionError("the cache directory is not part of the workspace API")
        return p

    # ── network ──────────────────────────────────────────────────────────────
    def _robots_ok(self, url: str) -> bool:
        parts = urllib.parse.urlsplit(url)
        base = f"{parts.scheme}://{parts.netloc}"
        rp = self._robots.get(base)
        if rp is None:
            rp = urllib.robotparser.RobotFileParser()
            try:
                req = urllib.request.Request(base + "/robots.txt", headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=10) as r:
                    rp.parse(r.read().decode("utf-8", "replace").splitlines())
            except urllib.error.HTTPError as e:
                rp.parse([] if e.code == 404 else ["User-agent: *", "Disallow: /"])
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                raise Transient(f"robots.txt unreachable: {e}") from e
            self._robots[base] = rp
        return rp.can_fetch(USER_AGENT, url)

    def _get(self, url: str) -> str:
        host = urllib.parse.urlsplit(url).netloc
        if host not in self.allow_hosts:
            raise PermissionError(f"host not on the allowlist: {host}")
        key = hashlib.sha256(url.encode()).hexdigest()[:24]
        cache_dir = self.ws / ".cache" / self.scope
        cache_dir.mkdir(parents=True, exist_ok=True)
        cached = cache_dir / key
        if cached.exists():                       # same URL in the same night: free
            return cached.read_text()
        if not self._robots_ok(url):
            raise PermissionError(f"robots.txt disallows {url}")
        if self.slow_s:
            time.sleep(self.slow_s)
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                text = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 429 or e.code >= 500:
                raise Transient(f"HTTP {e.code} from {host}") from e
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise Transient(f"network: {e}") from e
        cached.write_text(text)
        return text

    # ── tools ────────────────────────────────────────────────────────────────
    def pypi_releases(self, package: str) -> dict:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", package):
            raise ValueError(f"not a valid package name: {package!r}")
        url = f"{self.pypi_base}/rss/project/{package.lower()}/releases.xml"
        root = ET.fromstring(self._get(url))
        items = [(i.findtext("title", "").strip(), i.findtext("pubDate", "").strip(),
                  i.findtext("author", "").strip()) for i in root.iter("item")]
        stable = [(v, d) for v, d, _ in items if STABLE.match(v)]
        pre = [(v, d) for v, d, _ in items if not STABLE.match(v)]
        by_version = sorted(stable, key=lambda vd: tuple(int(x) for x in vd[0].split(".")), reverse=True)
        return {
            "package": package,
            "newest_stable": by_version[0][0] if by_version else None,
            "newest_prerelease": pre[0][0] if pre else None,
            "recent_stable": [{"version": v, "published": d} for v, d in stable[:5]],
            "maintainer_contact": items[0][2] if items else None,
            "source": url,
        }

    def fetch_url(self, url: str) -> dict:
        return {"url": url, "text": self._get(url)[:4000]}

    def read_file(self, path: str) -> dict:
        p = self._path(path)
        if not p.is_file():
            raise FileNotFoundError(f"{path} does not exist")
        return {"path": path, "content": p.read_text()}

    def write_file(self, path: str, content: str) -> dict:
        p = self._path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return {"path": path, "bytes": len(content.encode())}

    def list_files(self, path: str) -> dict:
        base = self._path(path)
        files = sorted(str(p.relative_to(self.ws)) for p in base.rglob("*")
                       if p.is_file() and ".cache" not in p.relative_to(self.ws).parts)
        return {"path": path, "files": files}

    def search_notes(self, query: str) -> dict:
        hits = []
        notes = self._path("notes")
        for p in sorted(notes.rglob("*")) if notes.exists() else []:
            if p.is_file():
                for n, line in enumerate(p.read_text(errors="replace").splitlines(), 1):
                    if query.lower() in line.lower():
                        hits.append(f"{p.relative_to(self.ws)}:{n}: {line[:200]}")
        return {"query": query, "hits": hits[:50]}

    def run_command(self, argv: list[str]) -> dict:
        if not argv or argv[0] not in ALLOWED_COMMANDS:
            raise PermissionError(f"command not allowlisted: {argv[:1]}")
        for a in argv[1:]:
            if a.startswith("/") or ".." in a:
                raise PermissionError(f"argument leaves the workspace: {a}")
        r = subprocess.run(argv, cwd=self.ws, capture_output=True, text=True, timeout=10, env={"PATH": "/usr/bin:/bin"})
        return {"argv": argv, "exit": r.returncode, "stdout": r.stdout[:4000], "stderr": r.stderr[:1000]}

    # internal op, not exposed to the model: the harness's progress log
    def _append(self, path: str, line: str) -> dict:
        p = self._path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as fh:
            fh.write(line.rstrip("\n") + "\n")
        return {"ok": True}

    def handle(self, req: dict) -> dict:
        self.scope = re.sub(r"[^A-Za-z0-9_-]", "_", req.get("scope") or "default")
        fn = getattr(self, req["tool"], None)
        if fn is None or (req["tool"].startswith("_") and req["tool"] != "_append"):
            return {"ok": False, "error": f"no such tool on this computer: {req['tool']}"}
        try:
            return {"ok": True, "result": fn(**req.get("args", {}))}
        except Transient as e:
            return {"ok": False, "transient": True, "error": str(e)}
        except (PermissionError, ValueError, FileNotFoundError, IsADirectoryError, ET.ParseError,
                urllib.error.HTTPError, subprocess.TimeoutExpired, TypeError) as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--pypi-base", required=True)
    ap.add_argument("--allow-host", action="append", default=[])
    ap.add_argument("--slow-s", type=float, default=float(os.environ.get("LRLAB_SLOW_TOOLS_S", "0")))
    a = ap.parse_args()
    comp = Computer(Path(a.workspace), a.pypi_base, set(a.allow_host), a.slow_s)
    for line in sys.stdin:
        req = json.loads(line)
        resp = comp.handle(req)
        resp["id"] = req.get("id")
        sys.stdout.write(json.dumps(resp) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
