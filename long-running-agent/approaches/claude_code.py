"""Approach C: Claude Code headless as the agent runtime.

`claude -p` brings its own harness and its own tools (Read, Write, Bash, ...). We give it the
workspace as its working directory, allow exactly the tools the job needs, cap turns and budget,
and resume the same session with `--resume <session_id>` when the human answers.

What we lose: our connector tool and our post-tool hooks (it fetches the raw RSS with curl and
parses XML itself), and send_to_user. Claude Code has no channel into our apps, so the
convention is a file: it writes outbox/message.json and ends its turn; this wrapper turns that
file into the same public event every app renders.

    python3 compare.py --approach claude-code --model claude-haiku-5-5
"""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

from lrlab.state import StateStore
from lrlab.task import SYSTEM_PROMPT, TASK_PROMPT, done_check

TALK = """
You have no send_to_user tool in this environment. Instead, to message the user write
outbox/message.json with {"kind": ..., "text": ..., "options": [...], "path": ...,
"expects_reply": bool, "final": bool} (same meaning as send_to_user), then END YOUR TURN.
There is no pypi_releases tool either: fetch {base}/rss/project/<name>/releases.xml and read
the <item><title> versions yourself. Ignore pre-releases (a, b, rc, dev).
Tool rules here: use Read and Write for every file. Bash is allowed for exactly one thing, a
single `curl -s <url>` command per call (no pipes, no &&, no other programs).
"""


def _claude(args: list[str], cwd: Path, timeout: int = 900) -> dict:
    r = subprocess.run(["claude", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout)
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {"is_error": True, "result": (r.stdout + r.stderr)[-500:]}


def run_claude_code(model: str, home: Path, web, truth: dict, grade) -> dict:
    if not shutil.which("claude"):
        return {"skipped": "claude CLI not installed"}
    ws = home / "workspace"
    (ws / "outbox").mkdir(parents=True, exist_ok=True)
    store = StateStore(home / "state.db")
    conv = store.conversation_for("claude-code/" + model)
    common = [
        "--output-format", "json",
        "--model", model,
        "--permission-mode", "dontAsk",
        "--allowedTools", f"Read Write Edit Bash(curl -s {web.base}/*)",
        "--tools", "Read,Write,Edit,Bash",
        "--setting-sources", "",
        "--strict-mcp-config",
        "--max-budget-usd", "1.0",
        "--append-system-prompt", SYSTEM_PROMPT + TALK.replace("{base}", web.base),
    ]
    t0 = time.monotonic()
    legs, flags = [], {}

    def drain() -> None:
        f = ws / "outbox" / "message.json"
        if not f.exists():
            return
        try:
            m = json.loads(f.read_text())
        except json.JSONDecodeError:
            store.event(conv, "tool_invalid", {"name": "outbox/message.json", "problem": "not valid JSON"})
            f.unlink()
            return
        f.unlink()
        read = lambda p: (ws / p).read_text() if (ws / p).is_file() else None
        if m.get("final"):
            problem = done_check(read)
            if problem:
                store.event(conv, "done_check_failed", {"problem": problem})
                return
            flags["final"] = True
        payload = {"kind": m.get("kind", "text"), "text": m.get("text", ""), "options": m.get("options")}
        store.event(conv, "message", payload, visibility="public")
        flags["waiting"] = bool(m.get("expects_reply")) and not m.get("final")

    legs.append(_claude(["-p", TASK_PROMPT.format(fire_time="2026-11-12T01:00:00+00:00"), *common], ws))
    drain()
    if flags.get("waiting") and legs[-1].get("session_id"):
        legs.append(_claude(["-p", "[webhook message from zdenek, provenance=user]\npin",
                             "--resume", legs[-1]["session_id"], *common], ws))
        drain()
    if not flags.get("final") and (ws / "report.md").exists() and done_check(
            lambda p: (ws / p).read_text() if (ws / p).is_file() else None) is None:
        flags["report_without_final_message"] = True
    status = "done" if flags.get("final") else ("parked_human" if flags.get("waiting") else
                                                 "error" if legs[-1].get("is_error") else "stopped")
    return {"status": status, "turns": sum(l.get("num_turns") or 0 for l in legs), "tokens": None,
            "cost_usd": round(sum(l.get("total_cost_usd") or 0 for l in legs), 4),
            "wall_s": round(time.monotonic() - t0, 1), **grade(home, store, truth),
            **({"note": "report written but no final message"} if flags.get("report_without_final_message") else {})}
