"""Live updates: Server-Sent Events over the public event log.

GET /events?conv=<id>&after=<seq>       stream public events as they are written
GET /events?conv=<id>&once=1            dump what exists and close (handy for curl and tests)
GET /                                   a tiny page that shows a conversation live

Because every app reads the same log, a phone and a laptop never disagree about what happened.
A reconnecting client sends Last-Event-ID and resumes exactly where it left off.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .state import StateStore

PAGE = """<!doctype html><meta charset=utf-8><title>agent live</title>
<body style="font:15px system-ui;max-width:720px;margin:2rem auto;padding:0 16px">
<h3>conversation <code id=c></code></h3><div id=log></div>
<script>
const conv = new URLSearchParams(location.search).get('conv') || '';
document.getElementById('c').textContent = conv;
const es = new EventSource('/events?conv=' + encodeURIComponent(conv));
es.onmessage = e => { const p = JSON.parse(e.data); const d = document.createElement('p');
  d.textContent = `[${p.kind}] ${p.text}` + (p.options ? '  (' + p.options.join(' | ') + ')' : '');
  document.getElementById('log').append(d); };
</script>"""


class LiveServer:
    def __init__(self, db_path: Path, port: int = 0):
        self.db_path = db_path
        self.port = port
        self.server: ThreadingHTTPServer | None = None

    def start(self) -> "LiveServer":
        db_path = self.db_path

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                u = urllib.parse.urlsplit(self.path)
                q = dict(urllib.parse.parse_qsl(u.query))
                if u.path == "/":
                    body = PAGE.encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if u.path != "/events":
                    self.send_error(404)
                    return
                store = StateStore(db_path)      # one connection per client thread
                after = int(self.headers.get("Last-Event-ID") or q.get("after", 0))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                try:
                    while True:
                        for ev in store.events_after(after, q.get("conv"), public_only=True):
                            self.wfile.write(f"id: {ev['seq']}\ndata: {json.dumps(ev['payload'])}\n\n".encode())
                            after = ev["seq"]
                        self.wfile.flush()
                        if q.get("once"):
                            return
                        time.sleep(0.5)
                except (BrokenPipeError, ConnectionResetError):
                    return

        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
