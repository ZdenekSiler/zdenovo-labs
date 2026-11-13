"""Approach B: the Claude Agent SDK runs the loop; we keep everything around it.

What the SDK gives you: the agent loop, context management (compaction), hooks, sessions you
can resume by id, cost tracking, max_turns / max_budget_usd.
What it does not: the queue, the triggers, the vault, the delivery to your apps, the done check
as policy, and a computer that is not your own laptop. Those stay ours, so the SDK slots in as
"the model + loop" box and nothing else changes.

    uv run --with claude-agent-sdk python compare.py --approach sdk --model claude-haiku-5-5
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

from lrlab.hooks import OutputCap, ProvenanceTagger, Redactor, render_result
from lrlab.runner import CommandRunner
from lrlab.state import StateStore
from lrlab.task import SYSTEM_PROMPT, TASK_PROMPT, done_check
from lrlab.tools import default_tools

SERVER = "lab"


def build_server(runner: CommandRunner, store: StateStore, conv: str, flags: dict):
    from claude_agent_sdk import create_sdk_mcp_server, tool

    registry = default_tools()
    post = [Redactor(), ProvenanceTagger(), OutputCap()]
    sdk_tools = []

    def read(path: str):
        r = runner.call("read_file", {"path": path}, scope=conv)
        return r["result"]["content"] if r["ok"] else None

    for spec in registry.specs.values():
        if spec.permission == "shell":
            continue                       # same policy as the harness: no shell on scheduled runs
        schema = spec.schema()["parameters"]

        def make(spec=spec):
            async def handler(args):
                store.event(conv, "tool_call", {"name": spec.name, "args": str(args)[:120]})
                if spec.name == "send_to_user":
                    return await asyncio.to_thread(send, args)
                r = await asyncio.to_thread(runner.call, spec.name, args, spec.timeout_s, conv)
                text = render_result(r["result"]) if r["ok"] else f"error: {r['error']}"
                for p in post:
                    text = p.transform({"args": args}, spec, text)
                store.event(conv, "tool_result", {"name": spec.name, "ok": r["ok"], "preview": text[:160]})
                return {"content": [{"type": "text", "text": text}], "is_error": not r["ok"]}
            return tool(spec.name, spec.description, schema)(handler)

        sdk_tools.append(make())

    def send(args):
        if args.get("final"):
            problem = done_check(read)
            if problem:
                store.event(conv, "done_check_failed", {"problem": problem})
                return {"content": [{"type": "text", "text": f"not sent: {problem}"}], "is_error": True}
            flags["final"] = True
        payload = {"kind": args["kind"], "text": args["text"], "options": args.get("options")}
        if args["kind"] == "file":
            payload["attachment"] = {"name": args.get("path"), "content": read(args.get("path", "")) or ""}
        store.event(conv, "message", payload, visibility="public")
        if args.get("expects_reply"):
            flags["waiting"] = True
            return {"content": [{"type": "text", "text": "delivered. End your turn now; the reply arrives later."}]}
        return {"content": [{"type": "text", "text": "delivered."}]}

    return create_sdk_mcp_server(name=SERVER, version="1.0.0", tools=sdk_tools)


async def _leg(prompt: str, model: str, home: Path, server, store, conv, session: str | None) -> dict:
    from claude_agent_sdk import ClaudeAgentOptions, HookMatcher, ResultMessage, query

    async def only_our_tools(input_data, tool_use_id, context):
        name = input_data["tool_name"]
        if not name.startswith(f"mcp__{SERVER}__"):
            store.event(conv, "tool_denied", {"name": name, "reason": "built-in tools are off for this agent"})
            return {"hookSpecificOutput": {"hookEventName": input_data["hook_event_name"],
                                           "permissionDecision": "deny",
                                           "permissionDecisionReason": "Only the lab tools are available."}}
        return {}

    options = ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        model=model,
        tools=[],                                    # no built-in Bash/Read/Write: the computer is ours
        mcp_servers={SERVER: server},
        allowed_tools=[f"mcp__{SERVER}__*"],
        permission_mode="dontAsk",
        hooks={"PreToolUse": [HookMatcher(hooks=[only_our_tools])]},
        cwd=str(home / "workspace"),
        max_turns=30,
        max_budget_usd=1.0,
        resume=session,
        setting_sources=[],                          # don't inherit the host's CLAUDE.md / hooks
    )
    result = {}
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, ResultMessage):
            result = {"session_id": msg.session_id, "cost": msg.total_cost_usd or 0.0,
                      "turns": msg.num_turns, "subtype": msg.subtype}
    return result


def run_sdk(model: str, home: Path, web, truth: dict, grade) -> dict:
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError:
        return {"skipped": "pip install claude-agent-sdk"}
    store = StateStore(home / "state.db")
    conv = store.conversation_for("sdk/" + model)
    runner = CommandRunner(home / "workspace", web.base)
    flags: dict = {}
    server = build_server(runner, store, conv, flags)
    t0 = time.monotonic()
    legs = []
    try:
        legs.append(asyncio.run(_leg(TASK_PROMPT.format(fire_time="2026-11-12T01:00:00+00:00"),
                                     model, home, server, store, conv, None)))
        if flags.get("waiting") and not flags.get("final"):
            # the night ends here; in production the session id sits in the state store until the reply
            store.event(conv, "parked", {"session_id": legs[-1].get("session_id")})
            legs.append(asyncio.run(_leg("[webhook message from zdenek, provenance=user]\npin",
                                         model, home, server, store, conv, legs[-1].get("session_id"))))
    finally:
        runner.close()
    status = "done" if flags.get("final") else ("parked_human" if flags.get("waiting") else
                                                 (legs[-1].get("subtype") if legs else "error"))
    return {"status": status, "turns": sum(l.get("turns") or 0 for l in legs), "tokens": None,
            "cost_usd": round(sum(l.get("cost") or 0 for l in legs), 4),
            "wall_s": round(time.monotonic() - t0, 1), **grade(home, store, truth)}
