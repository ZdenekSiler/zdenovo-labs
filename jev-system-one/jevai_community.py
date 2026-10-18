"""Part 6: the jevai.org community presets, called over REST.

jevai.org describes itself as "a community site for Jev AI extras". Its own homepage says the
official docs and API live with TypeSafe AI, and the site doesn't say who operates it.
Its endpoints take *business fields* (tool, action, side_effects...), build the Jev
questions server-side, and return {code, message, data}. You need a separate key from
https://www.jevai.org/agent/keys - a TypeSafe key won't work.

Read the trust note in part 6 before sending it anything you wouldn't post publicly.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import httpx2

BASE = "https://www.jevai.org"  # www only: the apex redirects, and many clients turn POST into GET


@dataclass
class ToolGuardRequest:
  tool: str
  action: str
  arguments_summary: list[str] = field(default_factory=list)
  side_effects: list[str] = field(default_factory=list)
  safeguards: list[str] = field(default_factory=list)
  policy: list[str] = field(default_factory=list)
  reversibility: str | None = None  # e.g. "partially_reversible"

  def payload(self) -> dict:
    return {k: v for k, v in self.__dict__.items() if v not in (None, [])}


class JevAICommunityClient:
  def __init__(self, api_key: str | None = None, timeout: float = 30.0,
               transport: httpx2.BaseTransport | None = None) -> None:
    key = api_key or os.environ["JEV_API_KEY"]
    self.http = httpx2.Client(base_url=BASE, timeout=timeout, transport=transport,
                              headers={"Authorization": f"Bearer {key}"})

  def _post(self, path: str, body: dict) -> dict:
    r = self.http.post(path, json=body)
    r.raise_for_status()
    envelope = r.json()
    if envelope.get("code") != 0:
      raise RuntimeError(f"jevai.org error {envelope.get('code')}: {envelope.get('message')}")
    return envelope["data"]

  def guard_tool_call(self, req: ToolGuardRequest) -> dict:
    return self._post("/api/v1/decisions/tool-guard", req.payload())

  def check_research(self, claim: str, evidence: list[str], source_quality: str = "mixed",
                     stakes: str = "medium") -> dict:
    return self._post("/api/v1/decisions/research", {"claim": claim, "evidence": evidence,
                                                     "source_quality": source_quality, "stakes": stakes})

  def decide(self, state, questions: dict) -> dict:
    """Native mode: same question JSON as TypeSafe's /v1/systemone."""
    return self._post("/api/v1/decisions", {"state": state, "questions": questions})
