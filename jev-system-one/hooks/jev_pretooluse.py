#!/usr/bin/env python3
"""Claude Code PreToolUse hook: screen outbound/destructive tool calls with Jev.

Register in .claude/settings.json:
  {"hooks": {"PreToolUse": [{"matcher": "Bash|WebFetch|mcp__.*",
     "hooks": [{"type": "command", "command": "uv run --project /path/to/lab python /path/to/lab/hooks/jev_pretooluse.py"}]}]}}

Exit 0 = allow. Exit 2 = block; stderr is shown to Claude as the reason.
The hook runs in the harness, so an injected agent can't decide to skip it - unlike a guard
exposed as an MCP tool, which the agent calls only if it wants to.
Fails OPEN on Jev errors by default (set JEV_HOOK_FAIL_CLOSED=1 to fail closed).
"""
from __future__ import annotations

import json
import os
import sys

from typesafe_sdk import Noul, NoulCriteria, RetryPolicy, Score, TypeSafeClient, TypeSafeError

QUESTIONS = {
  "sends_data_out": Noul(
    instructions="Would running `call` send file contents, environment variables, credentials or "
                 "conversation text to a host outside the local machine?",
    criteria=NoulCriteria(true="It uploads, posts, pipes or embeds local data in a request to a remote host",
                          false="It only reads public data, or acts locally"),
  ),
  "destructive": Score(
    instructions="How much damage could `call` do if it was not what the user wanted?",
    criteria=["None: read-only", "Minor: easily undone local change",
              "Major: deletes or overwrites work, changes remote state", "Severe: irreversible or affects shared systems"],
  ),
}


def decide(event: dict, client: TypeSafeClient) -> tuple[int, str]:
  call = {"tool": event.get("tool_name"), "input": event.get("tool_input")}
  r = client.system_one({"call": call}, QUESTIONS)
  out, harm = r.nouls["sends_data_out"].noul, r.scores["destructive"].score
  if out >= 0.7:
    return 2, f"Blocked by Jev screen: likely data egress (p={out:.2f}). Ask the user to run it if intended."
  if harm >= 2.0:
    return 2, f"Blocked by Jev screen: high-impact action (severity {harm:.1f}/3). Confirm with the user first."
  return 0, ""


def main(client: TypeSafeClient | None = None) -> int:
  event = json.load(sys.stdin)
  try:
    client = client or TypeSafeClient(timeout=2.0, retry=RetryPolicy(max_retries=1, timeout=3.0))
    code, reason = decide(event, client)
  except TypeSafeError as e:
    if os.environ.get("JEV_HOOK_FAIL_CLOSED") == "1":
      print(f"Jev screen unavailable ({type(e).__name__}); failing closed.", file=sys.stderr)
      return 2
    return 0
  if reason:
    print(reason, file=sys.stderr)
  return code


if __name__ == "__main__":
  sys.exit(main())
