"""Live-or-replay client factory.

With TYPESAFE_API_KEY set, requests go to the real API and every response is recorded to
cassettes/<name>.json. Without a key, the same code replays that cassette through an
httpx2.MockTransport, so every example in the series runs offline.

IMPORTANT: the cassettes shipped with the lab were hand-written to the documented response
shape (several are copied from docs.typesafe.ai examples). They exercise *your* code -
routing, thresholds, confidence maths - not the model. Re-record them with a key before
trusting any number that comes out of the model.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import httpx2
from typesafe_sdk import TypeSafeClient

CASSETTES = Path(__file__).parent / "cassettes"


def _key(body: dict) -> str:
  """Cassette key = the question ids + a hash of the state. Stable across runs."""
  qids = ",".join(sorted(body["questions"]))
  state = hashlib.sha256(json.dumps(body["state"], sort_keys=True).encode()).hexdigest()[:10]
  return f"{qids}|{state}"


class Cassette:
  def __init__(self, name: str) -> None:
    self.path = CASSETTES / f"{name}.json"
    self.entries: dict[str, dict] = json.loads(self.path.read_text()) if self.path.exists() else {}

  def save(self) -> None:
    CASSETTES.mkdir(exist_ok=True)
    self.path.write_text(json.dumps(self.entries, indent=2, sort_keys=True) + "\n")

  def replay_transport(self) -> httpx2.MockTransport:
    def handler(request: httpx2.Request) -> httpx2.Response:
      body = json.loads(request.content)
      key = _key(body)
      if key not in self.entries:
        # Fall back to a question-id match: lets one fixture serve many states.
        qids = key.split("|")[0]
        match = next((v for k, v in self.entries.items() if k.split("|")[0] == qids), None)
        if match is None:
          return httpx2.Response(404, json={"error": f"no cassette entry for {key}"})
        return httpx2.Response(200, json=match, headers={"x-typesafe-request-id": "req_replay"})
      return httpx2.Response(200, json=self.entries[key], headers={"x-typesafe-request-id": "req_replay"})

    return httpx2.MockTransport(handler)

  def recording_transport(self) -> httpx2.BaseTransport:
    inner = httpx2.HTTPTransport()
    cassette = self

    class Recorder(httpx2.BaseTransport):
      def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        response = inner.handle_request(request)
        response.read()
        if response.status_code == 200:
          cassette.entries[_key(json.loads(request.content))] = response.json()
          cassette.save()
        return response

    return Recorder()


def make_client(cassette: str, **kwargs) -> tuple[TypeSafeClient, str]:
  """Return (client, mode). mode is 'live' or 'replay' so demos can label their output."""
  tape = Cassette(cassette)
  if os.environ.get("TYPESAFE_API_KEY"):
    return TypeSafeClient(transport=tape.recording_transport(), **kwargs), "live"
  return TypeSafeClient(api_key="replay-only", transport=tape.replay_transport(), **kwargs), "replay"
