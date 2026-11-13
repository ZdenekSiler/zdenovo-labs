"""The harness: everything around the model that turns one chat turn into a job that survives
the night. agent = model + harness.

    loop control        budgets (turns, tokens, wall clock, money) and stop conditions
    tool registry       schemas the model sees, permissions and timeouts it doesn't
    hooks               pre-tool policy, post-tool redaction / provenance / caps
    one talking tool    send_to_user: the only path to a human; plain text stays private
    done check          "final" is a claim; the harness verifies it before anyone hears about it
    stop check          "quiet" is a claim too: stopping without a message must be legitimate
    journal+checkpoint  every tool result is journaled, every step checkpointed (fenced)
    context restart     over the context budget -> fresh context from the progress file,
                        never an in-place edit of old turns (history stays append-only)
    observability       every step is an event; apps render the public ones

The model is called through one method. Everything else here is deterministic code.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from .config import Budget
from .conv_queue import ConversationQueue, Lease
from .hooks import OutputCap, PermissionPolicy, ProvenanceTagger, Redactor, RunContext, SendToUserGuard, render_result
from .models.base import ModelClient, ModelError, ModelReply, estimate_tokens
from .runner import CommandRunner
from .state import LeaseLost, StateStore
from .tools import ToolRegistry

TRUST = {"untrusted": 0, "user": 1, "operator": 2}


@dataclass
class Outcome:
    status: str                 # done | quiet | parked_human | parked_retry | parked_budget | failed | lease_lost
    reason: str = ""
    retry_at: float | None = None
    model_calls: int = 0
    tool_calls: int = 0


class _Transient(Exception):
    pass


@dataclass
class AgentHarness:
    store: StateStore
    queue: ConversationQueue
    runner: CommandRunner
    model: ModelClient
    tools: ToolRegistry
    budget: Budget
    system_prompt: str
    done_check: Callable[[Callable[[str], str | None]], str | None] | None = None
    quiet_check: Callable[[Callable[[str], str | None], list[dict]], str | None] | None = None
    max_nudges: int = 2
    policy: PermissionPolicy = field(default_factory=PermissionPolicy)
    retry_base_s: float = 1.0
    park_retry_s: float = 300.0
    sleep: Callable[[float], None] = time.sleep

    def __post_init__(self):
        self.guards = [self.policy, SendToUserGuard()]
        self.redactor = Redactor()
        self.post = [self.redactor, ProvenanceTagger(), OutputCap()]

    # ── entry point ──────────────────────────────────────────────────────────
    def run(self, lease: Lease) -> Outcome:
        conv = lease.conv_id
        out = Outcome("running")
        st = self.store.load_checkpoint(conv) or {
            "messages": [], "context_no": 1, "task": None, "provenance": "operator",
            "usage": {"turns": 0, "in": 0, "out": 0, "cost": 0.0, "cost_unknown": False, "active_s": 0.0},
        }
        inbound = self.store.unconsumed_inbound(conv)
        for m in inbound:
            st["messages"].append({"role": "user", "content": self._frame(m)})
            st["task"] = st["task"] or m["content"]
            if len(st["messages"]) == 1:
                st["provenance"] = m["provenance"]
            elif TRUST[m["provenance"]] < TRUST[st["provenance"]]:
                st["provenance"] = m["provenance"]          # trust only ever goes down
        if inbound:
            waiting = self.store.conversation(conv)["waiting_for"]
            if waiting and waiting.get("kind") != "secret":
                self.store.set_waiting(conv, None)
        self._mark = time.monotonic()          # active time is counted from here, once
        self.store.event(conv, "run_start", {"inbound": len(inbound), "model": self.model.name,
                                             "resumed_messages": len(st["messages"]) - len(inbound)})
        try:
            self.store.save_checkpoint(conv, lease.fence, st, [m["seq"] for m in inbound])
            return self._loop(lease, st, out)
        except LeaseLost as e:
            self.store.event(conv, "lease_lost", {"error": str(e)})
            out.status, out.reason = "lease_lost", str(e)
            return out

    # ── the loop ─────────────────────────────────────────────────────────────
    def _loop(self, lease: Lease, st: dict, out: Outcome) -> Outcome:
        conv = lease.conv_id
        ctx = RunContext(conv, st["provenance"])
        while True:
            if not self.queue.renew(lease):
                raise LeaseLost("lease expired or taken over")
            over = self._over_budget(st["usage"])
            if over:
                return self._park_budget(lease, st, out, over)

            pending = self._pending_calls(st["messages"])
            if not pending:
                last = st["messages"][-1] if st["messages"] else None
                if last and last["role"] == "assistant":         # resumed after a quiet finish
                    out.status = "quiet"
                    return out
                if estimate_tokens(st["messages"]) > self.budget.context_tokens:
                    self._fresh_context(conv, st)
                try:
                    reply = self._call_model(conv, st)
                except _Transient as e:
                    self._checkpoint(lease, st)
                    return self._finish(conv, out, "parked_retry", str(e), time.time() + self.park_retry_s)
                except ModelError as e:
                    self._alert(conv, f"The agent stopped: the model provider returned a permanent error ({e}).")
                    self._checkpoint(lease, st)
                    return self._finish(conv, out, "failed", str(e))
                out.model_calls += 1
                st["messages"].append({
                    "role": "assistant", "content": reply.text, "raw": reply.raw,
                    "tool_calls": [{"id": c.id, "name": c.name, "args": c.args} for c in reply.tool_calls],
                })
                if reply.text:
                    self.store.event(conv, "model_text", {"text": reply.text[:500]})   # private
                self._checkpoint(lease, st)
                if not reply.tool_calls:
                    problem = self._stop_problem(reply.text, st["messages"])
                    if problem and st.get("nudges", 0) < self.max_nudges:
                        st["nudges"] = st.get("nudges", 0) + 1
                        st["messages"].append({"role": "user", "content": f"[harness] {problem}"})
                        self.store.event(conv, "stop_blocked", {"problem": problem, "nudge": st["nudges"]})
                        self._checkpoint(lease, st)
                        continue
                    self.store.event(conv, "run_end", {"why": "model stopped without messaging (quiet)"})
                    return self._finish(conv, out, "quiet", "no tool calls" + (f"; {problem}" if problem else ""))
                pending = st["messages"][-1]["tool_calls"]

            final = expects_reply = False
            for call in pending:
                try:
                    msg = self._execute(lease, ctx, call)
                except _Transient as e:
                    self._checkpoint(lease, st)
                    return self._finish(conv, out, "parked_retry", str(e), time.time() + self.park_retry_s)
                out.tool_calls += 1
                st["messages"].append(msg)
                self._checkpoint(lease, st)
                final |= msg.get("final_accepted", False)
                expects_reply |= msg.get("expects_reply", False)
            if final:   # "one less step": the last message ends the run without another model call
                self.store.event(conv, "run_end", {"why": "final message delivered"})
                return self._finish(conv, out, "done", "final")
            if expects_reply:
                self.store.event(conv, "parked", {"why": "waiting for the user"})
                return self._finish(conv, out, "parked_human", "waiting for reply")

    # ── model ────────────────────────────────────────────────────────────────
    def _call_model(self, conv: str, st: dict) -> ModelReply:
        last: ModelError | None = None
        for attempt in range(4):
            try:
                reply = self.model.complete(self.system_prompt, st["messages"], self.tools.schemas())
            except ModelError as e:
                last = e
                self.store.event(conv, "model_error", {"error": str(e)[:300], "transient": e.transient,
                                                       "attempt": attempt + 1})
                if not e.transient:
                    raise
                if attempt < 3:
                    self.sleep(e.retry_after or self.retry_base_s * 2 ** attempt)
                continue
            u = st["usage"]
            u["turns"] += 1
            u["in"] += reply.input_tokens
            u["out"] += reply.output_tokens
            if reply.cost_usd is None:
                u["cost_unknown"] = True
            else:
                u["cost"] += reply.cost_usd
            return reply
        raise _Transient(f"model unavailable after 4 attempts: {last}")

    # ── tools ────────────────────────────────────────────────────────────────
    def _execute(self, lease: Lease, ctx: RunContext, call: dict) -> dict:
        conv = lease.conv_id
        name, args = call["name"], call["args"]
        self.store.event(conv, "tool_call", {"id": call["id"], "name": name, "args": _short(args)})

        problem = self.tools.validate(name, args)
        if problem:
            self.store.event(conv, "tool_invalid", {"name": name, "problem": problem})
            return self._tool_msg(call, f"error: invalid call: {problem}", True)

        spec = self.tools.get(name)
        cached = self.store.journal_get(conv, call["id"])
        if cached is not None and spec.replay_safe:
            self.store.event(conv, "tool_replayed", {"name": name, "id": call["id"]})
            return cached

        for g in self.guards:
            d = g.check(call, spec, ctx)
            if not d.allow:
                self.store.event(conv, "tool_denied", {"name": name, "reason": d.reason})
                msg = self._tool_msg(call, f"error: denied by policy: {d.reason}", True)
                self.store.journal_put(conv, call["id"], name, args, msg)
                return msg

        if spec.runs_on == "harness":
            msg = self._send_to_user(conv, call)
        else:
            r = self.runner.call(name, args, spec.timeout_s, scope=conv)
            if not r["ok"] and r.get("transient"):
                self.store.event(conv, "tool_transient", {"name": name, "error": r["error"]})
                raise _Transient(f"{name}: {r['error']}")
            text = render_result(r["result"]) if r["ok"] else f"error: {r['error']}"
            for p in self.post:
                text = p.transform(call, spec, text)
            msg = self._tool_msg(call, text, not r["ok"])
        self.store.event(conv, "tool_result", {"name": name, "ok": not msg["is_error"],
                                               "preview": msg["content"][:160]})
        self.store.journal_put(conv, call["id"], name, args, msg)    # journal BEFORE checkpoint
        self.runner.call("_append", {"path": "notes/progress.md", "line": (
            f"- {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {name}({_short(args)}) -> "
            f"{'ERROR ' if msg['is_error'] else ''}{msg['content'][:100]!r}")})
        return msg

    def _send_to_user(self, conv: str, call: dict) -> dict:
        a = call["args"]
        if a.get("final") and self.done_check:
            problem = self.done_check(self._read)
            if problem:
                self.store.event(conv, "done_check_failed", {"problem": problem})
                return self._tool_msg(call, f"error: not sent. You said final, but the job is not done: {problem}. "
                                            "Finish the work, then send the final message again.", True)
        payload = {"kind": a["kind"], "text": self.redactor.transform(call, None, a["text"])}
        if a.get("options"):
            payload["options"] = a["options"]
        if a["kind"] == "file":
            content = self._read(a["path"])
            if content is None:
                return self._tool_msg(call, f"error: cannot attach {a['path']}: file not found", True)
            payload["attachment"] = {"name": a["path"], "content": self.redactor.transform(call, None, content)}
        expects = bool(a.get("expects_reply")) or a["kind"] == "secret"
        if a["kind"] == "secret":
            self.store.set_waiting(conv, {"kind": "secret", "secret_name": a["secret_name"]})
        elif expects:
            self.store.set_waiting(conv, {"kind": a["kind"], "options": a.get("options")})
        self.store.event(conv, "message", payload, visibility="public")
        note = "delivered."
        if expects:
            note += " The user's reply will arrive as a new message; end your turn now."
        msg = self._tool_msg(call, note, False)
        msg["final_accepted"] = bool(a.get("final"))
        msg["expects_reply"] = expects
        return msg

    def _read(self, path: str) -> str | None:
        r = self.runner.call("read_file", {"path": path})
        return r["result"]["content"] if r["ok"] else None

    def _stop_problem(self, text: str, messages: list[dict]) -> str | None:
        """The model stopped without a tool call. Is that a legitimate quiet end?"""
        t = (text or "").strip()
        if t.startswith("{") and '"name"' in t[:200]:
            return ("Your last reply contained a tool call written as plain text. Text is never executed "
                    "and never shown to the user. Call tools through the tool-calling interface.")
        if self.quiet_check:
            why = self.quiet_check(self._read, messages)
            if why:
                return (f"You stopped without calling send_to_user, but {why}. Your plain text is private: "
                        "the user saw nothing. Finish the job, or tell the user with send_to_user.")
        return None

    # ── context, budget, bookkeeping ─────────────────────────────────────────
    def _fresh_context(self, conv: str, st: dict) -> None:
        """Restart from files instead of editing history. The old turns stay in the checkpoint
        history of past contexts (events), the model just doesn't see them any more."""
        progress = (self._read("notes/progress.md") or "").splitlines()[-60:]
        listing = self.runner.call("list_files", {"path": "."})
        files = listing["result"]["files"] if listing["ok"] else []
        st["context_no"] += 1
        st["messages"] = [{"role": "user", "content": (
            f"{st['task']}\n\n[Fresh context #{st['context_no']}: earlier turns were dropped to stay within "
            f"the context budget. Your progress log (latest 60 lines):]\n" + "\n".join(progress) +
            f"\n\n[Workspace files:] {', '.join(files)}\nContinue the job from where the log ends.")}]
        self.store.event(conv, "context_restart", {"context_no": st["context_no"], "progress_lines": len(progress)})

    def _over_budget(self, u: dict) -> str | None:
        b = self.budget
        if u["turns"] >= b.max_turns:
            return f"turn budget ({b.max_turns}) used up"
        if u["in"] + u["out"] >= b.max_tokens:
            return f"token budget ({b.max_tokens:,}) used up"
        if u["cost"] >= b.max_cost_usd:
            return f"cost budget (${b.max_cost_usd:.2f}) used up"
        if u["active_s"] >= b.max_wall_s:
            return f"wall-clock budget ({b.max_wall_s:.0f}s) used up"
        return None

    def _park_budget(self, lease: Lease, st: dict, out: Outcome, why: str) -> Outcome:
        self._alert(lease.conv_id, f"I paused the job: {why}. Progress is saved in notes/progress.md. "
                                   "Reply 'continue' to grant another budget, or 'stop'.")
        st["usage"].update(turns=0, **{"in": 0, "out": 0}, cost=0.0, active_s=0.0)  # a reply grants a new budget
        self.store.save_checkpoint(lease.conv_id, lease.fence, st)
        return self._finish(lease.conv_id, out, "parked_budget", why)

    def _alert(self, conv: str, text: str) -> None:
        """The harness may talk to the user directly about the harness (never on the model's behalf)."""
        self.store.event(conv, "message", {"kind": "alert", "text": text}, visibility="public")

    def _checkpoint(self, lease: Lease, st: dict) -> None:
        now = time.monotonic()
        st["usage"]["active_s"] += now - self._mark    # each interval once; waiting while parked is free
        self._mark = now
        self.store.save_checkpoint(lease.conv_id, lease.fence, st)

    def _finish(self, conv: str, out: Outcome, status: str, reason: str, retry_at: float | None = None) -> Outcome:
        out.status, out.reason, out.retry_at = status, reason, retry_at
        self.store.event(conv, "run_outcome", {"status": status, "reason": reason})
        return out

    @staticmethod
    def _pending_calls(messages: list[dict]) -> list[dict]:
        for i in range(len(messages) - 1, -1, -1):
            if messages[i]["role"] == "assistant":
                answered = {m["call_id"] for m in messages[i + 1:] if m["role"] == "tool"}
                return [c for c in messages[i].get("tool_calls", []) if c["id"] not in answered]
        return []

    @staticmethod
    def _frame(m: dict) -> str:
        head = f"[{m['channel']} message from {m['sender']}, provenance={m['provenance']}]"
        if m["provenance"] == "untrusted":
            return f"{head}\n<untrusted_data>\n{m['content']}\n</untrusted_data>"
        return f"{head}\n{m['content']}"

    @staticmethod
    def _tool_msg(call: dict, content: str, is_error: bool) -> dict:
        return {"role": "tool", "call_id": call["id"], "name": call["name"], "content": content, "is_error": is_error}


def _short(args) -> str:
    if not isinstance(args, dict):
        return repr(args)[:80]
    parts = []
    for k, v in args.items():
        s = v if isinstance(v, str) else repr(v)
        parts.append(f"{k}={s[:40] + '…' if len(s) > 40 else s}")
    return ", ".join(parts)
