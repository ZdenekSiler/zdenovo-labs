"""A scripted model, so the whole system runs offline and gives the same transcript every time.

It is not a list of canned replies. It derives its next step from the conversation history, the
way a real model would, which is what makes crash-and-resume testable: kill the worker anywhere,
restart it, and the fake picks up from whatever the checkpoint says happened.
"""
from __future__ import annotations

import hashlib
import json

from ..task import classify, extract_json, parse_lockfile
from .base import ModelReply, ToolCall, estimate_tokens


class ScriptedResearcher:
    name = "fake:scripted"

    def __init__(self, batch: int = 4, lazy_finish: bool = False):
        self.batch = batch
        self.lazy_finish = lazy_finish      # chaos drill: claim "done" before doing the work
        self.calls = 0

    def complete(self, system: str, messages: list[dict], tools: list[dict]) -> ModelReply:
        self.calls += 1
        reply = self._decide(messages)
        reply.input_tokens = estimate_tokens(messages) + len(system) // 4
        reply.output_tokens = 60 + 20 * len(reply.tool_calls)
        return reply

    # ── reading the history ──────────────────────────────────────────────────
    @staticmethod
    def _results(messages: list[dict]) -> list[tuple[str, dict, str, bool]]:
        args_by_id = {}
        for m in messages:
            if m["role"] == "assistant":
                for c in m.get("tool_calls", []):
                    args_by_id[c["id"]] = (c["name"], c["args"])
        out = []
        for m in messages:
            if m["role"] == "tool" and m["call_id"] in args_by_id:
                name, args = args_by_id[m["call_id"]]
                out.append((name, args, m["content"], bool(m.get("is_error"))))
        return out

    def _call(self, name: str, **args) -> ToolCall:
        digest = hashlib.sha256(json.dumps([name, args], sort_keys=True).encode()).hexdigest()[:6]
        return ToolCall(f"fk_{self.calls}_{digest}", name, args)

    def _decide(self, messages: list[dict]) -> ModelReply:
        res = self._results(messages)
        reads = {a.get("path"): (c, err) for n, a, c, err in res if n == "read_file"}
        if self.lazy_finish and not any(n == "send_to_user" for n, *_ in res):
            return ModelReply("Looks fine, wrapping up.", [self._call(
                "send_to_user", kind="text", text="All dependencies are fine.", final=True)])
        if "lockfile.txt" not in reads:
            return ModelReply("Start: read the lockfile and last night's snapshot.", [
                self._call("read_file", path="lockfile.txt"),
                self._call("read_file", path="notes/last_seen.json")])

        lock = parse_lockfile(extract_json(reads["lockfile.txt"][0])["content"])
        if not any(n == "run_command" for n, *_ in res):
            return ModelReply("Check what notes exist from earlier runs.",
                              [self._call("run_command", argv=["ls", "notes"])])
        if not any(n == "list_files" for n, *_ in res):
            return ModelReply("Shell is not allowed on scheduled runs; use list_files instead.",
                              [self._call("list_files", path=".")])

        latest: dict[str, str] = {}
        for n, a, c, err in res:
            if n == "pypi_releases" and not err:
                data = extract_json(c)
                if data and data.get("newest_stable"):
                    latest[a["package"]] = data["newest_stable"]
        todo = [p for p, _ in lock if p not in latest]
        if todo:
            nxt = todo[: self.batch]
            return ModelReply(f"{len(latest)}/{len(lock)} looked up; next: {', '.join(nxt)}.",
                              [self._call("pypi_releases", package=p) for p in nxt])

        seen_raw, seen_err = reads.get("notes/last_seen.json", ("", True))
        seen = {} if seen_err else json.loads(extract_json(seen_raw)["content"])
        if seen and all(seen.get(p) == latest[p] for p, _ in lock):
            return ModelReply("Nothing changed since the last run. Staying quiet.", [])

        changes = {p: classify(v, latest[p]) for p, v in lock}
        majors = [p for p, _ in lock if changes[p] == "major"]
        asked = any(n == "send_to_user" and a.get("kind") == "choice" for n, a, *_ in res)
        replies = [m["content"] for m in messages[1:] if m["role"] == "user"]
        answer = replies[-1] if asked and replies else None
        if majors and not asked:
            names = ", ".join(f"{p} {dict(lock)[p]} -> {latest[p]}" for p in majors)
            return ModelReply(f"{len(majors)} breaking-looking upgrades; asking before I write anything.", [
                self._call("send_to_user", kind="choice", expects_reply=True,
                           text=f"{len(majors)} dependencies have a major/0.x-minor bump: {names}. What should I do?",
                           options=["pin", "upgrade-branch", "ignore"])])
        if majors and asked and answer is None:
            return ModelReply("Still waiting for the user's answer.", [])

        wrote = {a.get("path") for n, a, c, err in res if n == "write_file" and not err}
        if "report.md" not in wrote:
            rows = "\n".join(f"| {p} | {v} | {latest[p]} | {changes[p]} |" for p, v in lock)
            counts = {k: sum(1 for c in changes.values() if c == k) for k in ("major", "minor", "patch", "same")}
            decision = ""
            if answer:
                choice = next((o for o in ("upgrade-branch", "pin", "ignore") if o in answer.lower()), answer.strip())
                decision = f"\n\nDecision: {choice} ({', '.join(majors)}), answered by the user.\n"
            report = (f"# Dependency watch\n\n{counts['major']} major, {counts['minor']} minor, "
                      f"{counts['patch']} patch, {counts['same']} unchanged.\n\n"
                      f"| package | locked | latest | change |\n|---|---|---|---|\n{rows}\n{decision}")
            return ModelReply("All looked up. Writing the report and the snapshot.", [
                self._call("write_file", path="report.md", content=report),
                self._call("write_file", path="notes/last_seen.json",
                           content=json.dumps({p: latest[p] for p, _ in lock}, indent=1))])

        n_major = sum(1 for c in changes.values() if c == "major")
        n_other = sum(1 for c in changes.values() if c in ("minor", "patch"))
        return ModelReply("Done. Sending the report.", [self._call(
            "send_to_user", kind="file", path="report.md", final=True,
            text=f"Nightly dependency watch: {n_major} major, {n_other} minor/patch, report attached.")])
