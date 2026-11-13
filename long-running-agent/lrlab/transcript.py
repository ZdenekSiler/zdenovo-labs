"""Render the event log as a readable transcript (operator view: private + public events)."""
from __future__ import annotations

from .state import StateStore


class Transcript:
    """Prints the event log: private events (model text, tool calls) are what YOU see as the
    operator; public ones (messages) are what the user's apps show."""

    TERSE = {"model_text", "tool_denied", "message", "run_outcome", "tool_transient", "model_error",
             "done_check_failed", "tool_invalid", "stop_blocked", "lease_lost", "context_restart", "tool_replayed", "gateway_reject"}

    def __init__(self, store: StateStore, verbose: bool = True, printer=print):
        self.store, self.seq, self.verbose, self.printer = store, 0, verbose, printer

    def show(self) -> None:
        for ev in self.store.events_after(self.seq, public_only=False):
            self.seq = ev["seq"]
            k, p = ev["kind"], ev["payload"]
            if not self.verbose and k not in self.TERSE:
                continue
            line = {
                "run_start": lambda: f"  ── run start: model={p['model']}, inbound={p['inbound']}, "
                                     f"resumed_messages={p['resumed_messages']}",
                "model_text": lambda: f"     model (private): {p['text']}",
                "tool_call": lambda: f"     → {p['name']}({p['args']})",
                "tool_result": lambda: f"       ← {'ok' if p['ok'] else 'ERROR'}: {p['preview'].replace(chr(10), ' ')[:110]}"
                                       ,
                "tool_denied": lambda: f"       ✗ denied: {p['reason']}",
                "tool_replayed": lambda: f"       ↺ replayed from journal: {p['name']} ({p['id']})",
                "tool_invalid": lambda: f"       ✗ invalid call: {p['problem']}",
                "tool_transient": lambda: f"       ⚡ transient: {p['error']}",
                "stop_blocked": lambda: f"       ✗ stop check (nudge {p['nudge']}): {p['problem'][:120]}",
                "done_check_failed": lambda: f"       ✗ done check: {p['problem']}",
                "model_error": lambda: f"     ⚡ model error (attempt {p['attempt']}): {p['error'][:90]}",
                "message": lambda: f"     ✉ to user [{p['kind']}]: {p['text']}"
                                   + (f"  options={p['options']}" if p.get("options") else ""),
                "run_outcome": lambda: f"  ── run outcome: {p['status']} ({p['reason']})",
                "gateway_reject": lambda: f"  gateway REJECTED {p['channel']} from {p['sender']}: {p['reason']}",
                "inbound": lambda: f"  gateway accepted {p['channel']} from {p['sender']} "
                                   f"(provenance={p['provenance']})",
                "context_restart": lambda: f"     ⟳ fresh context #{p['context_no']} from progress log",
                "lease_lost": lambda: f"  ✗ lease lost: {p['error']}",
            }.get(k, lambda: None)()
            if line:
                self.printer(line)
