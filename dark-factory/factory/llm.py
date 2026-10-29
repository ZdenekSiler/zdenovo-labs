"""Tiny Ollama client. Stdlib only, so the lab has zero pip dependencies.

Every call returns JSON or raises. Timeouts and connection errors are Retryable; the queue
backs off and tries again later, which is exactly what you want when Ollama gets OOM-killed
at 3am and systemd restarts it.
"""
import json
import socket
import urllib.error
import urllib.request

from .errors import Fatal, Offline, Retryable, is_unreachable


class OllamaClient:
    def __init__(self, model: str, host: str = "http://localhost:11434", timeout: float = 180):
        self.model = model
        self.host = host.rstrip("/")
        self.timeout = timeout
        self.tokens = 0  # prompt + completion, summed for the morning report

    def chat_json(self, system: str, user: str, temperature: float = 0.0, model: str | None = None) -> dict:
        model = model or self.model
        body = json.dumps({
            "model": model,
            "stream": False,
            "format": "json",  # constrained decoding: always parseable, not always *right*
            "options": {"temperature": temperature},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }).encode()
        req = urllib.request.Request(
            f"{self.host}/api/chat", body, {"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise Fatal(f"model {model!r} not pulled (ollama pull {model})")
            raise Retryable(f"ollama HTTP {e.code}")
        except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
            # Refused or reset: Ollama isn't running (it got OOM-killed twice building this
            # lab). Pause the stage. A timeout is different: Ollama is there, just slow.
            if is_unreachable(e) or isinstance(e, ConnectionError):
                raise Offline("ollama", str(e))
            raise Retryable(f"ollama unreachable: {e}")
        self.tokens += data.get("prompt_eval_count", 0) + data.get("eval_count", 0)
        try:
            return json.loads(data["message"]["content"])
        except (KeyError, json.JSONDecodeError) as e:
            raise Retryable(f"ollama returned non-JSON: {e}")

    def ping(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.host}/api/tags", timeout=5) as resp:
                names = {m["name"] for m in json.load(resp)["models"]}
        except Exception:
            return False
        return self.model in names or f"{self.model}:latest" in names
