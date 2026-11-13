"""A local stand-in for pypi.org: serves the recorded RSS feeds (and PyPI's real robots.txt) from
fixtures/pypi, counts hits per path, and can be switched off to simulate an outage.

The feeds were recorded from https://pypi.org/rss/project/<name>/releases.xml on the date in
fixtures/pypi/RECORDED_AT, with maintainer e-mail addresses replaced by a placeholder.
"""
from __future__ import annotations

import threading
from collections import Counter
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class _Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, hits: Counter, **kw):
        self.hits = hits
        super().__init__(*a, **kw)

    def do_GET(self):
        self.hits[self.path] += 1
        super().do_GET()

    def log_message(self, *a):
        pass


class FixtureWeb:
    def __init__(self, root: Path, port: int = 0):
        self.root = root
        self.port = port
        self.hits: Counter = Counter()
        self.server: ThreadingHTTPServer | None = None

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "FixtureWeb":
        handler = partial(_Handler, hits=self.hits, directory=str(self.root))
        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.server = None

    def feed_hits(self) -> dict[str, int]:
        return {p.split("/")[3]: n for p, n in self.hits.items() if p.startswith("/rss/project/")}
