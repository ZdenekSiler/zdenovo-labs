"""PoliteFetcher: the only way any stage touches the network.

Three promises, each enforced in code rather than in a README:
  1. robots.txt is obeyed, including `*` wildcards (checks.robots_allows). A disallowed URL is
     Fatal: no retry, no workaround, the task dead-letters and a human reads why.
  2. At most one request per `delay` seconds per host, across ALL worker processes. A lock
     file plus a timestamp in work/.rate/ makes the limit global, not per worker.
  3. The User-Agent says who we are and where the code lives. No browser disguise: if a site
     only answers browsers, that is its answer.
"""
import fcntl
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import checks
from .errors import Fatal, Offline, Retryable, is_unreachable

USER_AGENT = "zdenovo-labs-dark-factory/1.0 (+https://github.com/ZdenekSiler/zdenovo-labs)"


class PoliteFetcher:
    def __init__(self, rate_dir: Path, delay: float = 1.0, timeout: float = 60):
        self.rate_dir = rate_dir
        self.rate_dir.mkdir(parents=True, exist_ok=True)
        self.delay = delay
        self.timeout = timeout
        self._robots: dict[str, dict | None] = {}
        self.notes: list[str] = []  # judgement calls made on the way, for the morning report

    # ── public ───────────────────────────────────────────────────────────────────
    def get(self, url: str) -> bytes | None:
        """Body of a 200, or None for 404/410 (a listing that's gone isn't an error)."""
        self._require_allowed(url)
        try:
            with self._open(url) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code in (404, 410):
                return None
            raise self._classify(e, url)

    def download(self, url: str, path: Path, open_data: bool = False) -> int:
        """Stream to path.part, resuming with a Range request if a previous attempt died
        halfway (the ČÚZK server dropped the 63 MB RÚIAN file at 56 MB on the first try).
        Renames into place only when complete; returns the size.

        open_data=True is for a dataset file its publisher documents for bulk download under
        an open licence. An explicit Disallow still wins. An *unreachable* robots.txt doesn't
        block it, but leaves a note."""
        self._require_allowed(url, open_data)
        part = path.with_suffix(path.suffix + ".part")
        have = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={have}-"} if have else {}
        try:
            with self._open(url, headers) as resp, open(part, "ab" if resp.status == 206 else "wb") as fh:
                expected = resp.headers.get("Content-Length")
                while chunk := resp.read(1 << 20):
                    fh.write(chunk)
        except urllib.error.HTTPError as e:
            if e.code == 416:  # range past the end: the .part is already complete
                os.replace(part, path)
                return path.stat().st_size
            raise self._classify(e, url)
        except (urllib.error.URLError, TimeoutError, ConnectionError, socket.timeout) as e:
            raise Retryable(f"download interrupted at {part.stat().st_size if part.exists() else 0:,} bytes: {e}")
        if expected and resp.status == 200 and part.stat().st_size != int(expected):
            raise Retryable(f"short download: {part.stat().st_size:,} of {int(expected):,} bytes")
        os.replace(part, path)
        return path.stat().st_size

    # ── internals ────────────────────────────────────────────────────────────────
    def _require_allowed(self, url: str, open_data: bool = False):
        parts = urllib.parse.urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        if origin not in self._robots:
            self._robots[origin] = self._load_robots(origin)
        rules = self._robots[origin]
        if rules is None:
            # RFC 9309: unreachable robots.txt means assume complete disallow, for crawlers.
            if not open_data:
                del self._robots[origin]  # ask again next attempt
                raise Retryable(f"robots.txt for {origin} unreachable; not crawling until it answers")
            # ČÚZK's server answers every path except /robots.txt, which dies with an HTTP/2
            # INTERNAL_ERROR. That's a broken server, not an opt-out, and the file is published
            # for exactly this download. A human made that call; the note makes it visible.
            self.notes.append(f"robots.txt for {origin} unreachable; downloaded the documented "
                              f"open-data file {parts.path} anyway")
            return
        path = parts.path + (f"?{parts.query}" if parts.query else "")
        if not checks.robots_allows(rules, USER_AGENT, path):
            raise Fatal(f"robots.txt disallows {url}")

    def _load_robots(self, origin: str) -> dict | None:
        """Rules, {} for a 4xx (no robots.txt = no rules), None if unreachable or 5xx."""
        try:
            with self._open(f"{origin}/robots.txt") as resp:
                return checks.parse_robots(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            return {} if 400 <= e.code < 500 else None
        except Offline:
            raise  # no network at all is not the same as a broken robots.txt
        except Retryable:  # _open wraps other network errors
            return None

    def _open(self, url: str, headers: dict | None = None):
        host = urllib.parse.urlsplit(url).netloc
        self._check_blackout(host)
        self._wait_turn(host)
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
        try:
            return urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError, socket.timeout) as e:
            if is_unreachable(e):
                raise Offline(host, str(e))
            raise Retryable(f"network: {e}")

    def _check_blackout(self, host: str):
        """`run.py blackout N` writes work/BLACKOUT: every fetch fails as if the network were
        gone for N seconds. It's how the offline handling gets tested without unplugging anything."""
        marker = self.rate_dir.parent / "BLACKOUT"
        try:
            until = float(marker.read_text())
        except (FileNotFoundError, ValueError):
            return
        if time.time() < until:
            raise Offline(host, "simulated blackout (run.py blackout)")

    def _wait_turn(self, host: str):
        """Global per-host rate limit: whoever holds the lock reads the last request time,
        sleeps until `delay` has passed, stamps the new time, releases."""
        lock_path = self.rate_dir / f"{host}.lock"
        with open(lock_path, "a+") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            fh.seek(0)
            last = float(fh.read() or 0)
            wait = last + self.delay - time.time()
            if wait > 0:
                time.sleep(wait)
            fh.seek(0)
            fh.truncate()
            fh.write(f"{time.time():.3f}")
            fh.flush()
            fcntl.flock(fh, fcntl.LOCK_UN)

    @staticmethod
    def _classify(e: urllib.error.HTTPError, url: str) -> Exception:
        if e.code in (429, 503):
            retry_after = e.headers.get("Retry-After", "?")
            return Retryable(f"HTTP {e.code} (Retry-After {retry_after}) from {url}: backing off")
        if e.code in (401, 403):
            return Fatal(f"HTTP {e.code} from {url}: access refused, not working around it")
        if 400 <= e.code < 500:
            return Fatal(f"HTTP {e.code} from {url}")
        return Retryable(f"HTTP {e.code} from {url}")
