"""The morning report: the only thing a human should need to read after an unattended run.
It answers four questions: what finished, what failed, what recovered by itself, and what
needs a decision."""
import json
import time
from collections import Counter, defaultdict

from .stages import PIPELINE
from .workspace import FAULTS, Workspace


def _hm(ts: float) -> str:
    return time.strftime("%H:%M", time.localtime(ts))


class MorningReport:
    def __init__(self, ws: Workspace):
        self.ws = ws

    def render(self) -> str:
        q = self.ws.queue
        gates = q.gates()
        lines = [f"# Morning report ({time.strftime('%Y-%m-%d %H:%M')})", ""]

        blocking_ok = all(gates.get(s.name, (False, []))[0] for s in PIPELINE if s.blocking)
        all_ok = all(gates.get(s.name, (False, []))[0] for s in PIPELINE)
        verdict = ("ALL GATES PASSED" if all_ok else
                   "RESULT DELIVERED, ENRICHMENT FAILED: NEEDS A HUMAN" if blocking_ok else "NEEDS A HUMAN")
        lines += [f"**Verdict:** {verdict}", ""]

        lines += ["| stage | done | dead | pending/running | gate |", "|---|---|---|---|---|"]
        for s in PIPELINE:
            c = q.counts(s.name)
            passed, problems = gates.get(s.name, (None, []))
            gate = "-" if passed is None else ("pass" if passed else "FAIL: " + "; ".join(problems))
            lines.append(f"| {s.name} | {c.get('done', 0)} | {c.get('dead', 0)} | "
                         f"{c.get('pending', 0) + c.get('running', 0)} | {gate} |")

        recovered = {k: 0 for k in ("retry", "worker_died", "lease_expired", "fenced", "self_correct",
                                    "offline", "host_suspended")}
        for _, _, _, kind, _ in q.events(tuple(recovered)):
            recovered[kind] += 1
        lines += ["", "## Recovered without you", "",
                  f"- {recovered['retry']} task attempts failed and were retried with backoff",
                  f"- {recovered['worker_died']} worker processes died and were restarted",
                  f"- {recovered['lease_expired']} tasks reclaimed after their worker's lease expired",
                  f"- {recovered['fenced']} late results discarded by fencing (a hung worker came back)",
                  f"- {recovered['self_correct']} LLM answers rejected by a grader and corrected",
                  f"- {recovered['offline']} tasks handed back while a dependency was offline (no attempt spent)",
                  f"- {recovered['host_suspended']} times the host was frozen or asleep (leases extended)"]
        lines += self._interruptions() + self._chaos()

        hist = q.attempts_histogram()
        lines += ["", "## Attempts per task", ""]
        for stage, h in hist.items():
            lines.append(f"- {stage}: " + ", ".join(f"{n}x{a}" for a, n in sorted(h.items())))

        dead = q.dead()
        lines += ["", "## Needs a decision (dead letters)", ""]
        for d in dead:
            lines.append(f"- `{d['id']}` after {d['attempts']} attempts:")
            lines += [f"  - {i}. {e[:160]}" for i, e in enumerate(q.history(d["id"]), 1)]
        if not dead:
            lines.append("- none")

        warnings = [w[4] for w in q.events(("warning",))]
        if warnings:
            lines += ["", "## Warnings", ""] + [f"- {w}" for w in warnings[-5:]]

        facts_path = self.ws.out / "facts.json"
        if facts_path.exists():
            facts = json.loads(facts_path.read_text())
            reduce_result = [r for _, r in q.results("reduce")]
            narrator = reduce_result[0]["narrator"] if reduce_result else "?"
            lines += ["", f"## Result (narrated by: {narrator})", "",
                      (self.ws.out / "summary.md").read_text().strip(), "",
                      "```json", json.dumps(facts, indent=1), "```"]
        return "\n".join(lines) + "\n"

    def _interruptions(self) -> list[str]:
        """Offline isn't a failure, but you want to know it happened and for how long."""
        q = self.ws.queue
        out = ["", "## Offline and interruptions", ""]
        events = q.events(("run_start", "halt", "host_suspended", "circuit_open", "circuit_closed"))
        runs = [e for e in events if e[3] == "run_start"]
        out.append(f"- {len(runs)} supervisor start(s) (the first run plus every resume)")
        for ts, _, _, kind, detail in events:
            if kind == "halt":
                out.append(f"- {_hm(ts)} halted: {detail}")
            elif kind == "host_suspended":
                out.append(f"- {_hm(ts)} woke up after {int(detail.split('s')[0]) / 60:.0f} min frozen: {detail.split('; ')[1]}")
            elif kind == "circuit_open":
                out.append(f"- {_hm(ts)} paused, {detail}")
            elif kind == "circuit_closed":
                out.append(f"- {_hm(ts)} resumed, {detail}")
        return out

    def _chaos(self) -> list[str]:
        """Every injected fault next to what became of the task it hit. Anything other than
        'done' here is either a bug in the factory or a gate doing its job: look."""
        q = self.ws.queue
        hits = q.events(("chaos",))
        if not hits:
            return []
        by_fault: dict[str, set[str]] = defaultdict(set)
        count = Counter()
        for _, task_id, _, _, detail in hits:
            fault = detail.split(":")[0]
            count[fault] += 1
            by_fault[fault].add(task_id)
        status = q.statuses(set().union(*by_fault.values()))
        out = ["", "## Chaos: injected vs recovered", "",
               "| fault | mimics | injected | tasks hit | ended done |", "|---|---|---|---|---|"]
        for fault in FAULTS:
            if count[fault]:
                done = sum(1 for t in by_fault[fault] if status.get(t) == "done")
                out.append(f"| {fault} | {FAULTS[fault]} | {count[fault]} | {len(by_fault[fault])} | {done} |")
        drills = [(ts, d) for ts, tid, _, _, d in hits if d.split(":")[0] not in FAULTS]
        if drills:
            out += ["", "Drills run against the live process:", ""]
            out += [f"- {_hm(ts)} {d}" for ts, d in drills]
        return out

    def write(self) -> str:
        text = self.render()
        (self.ws.out / "MORNING_REPORT.md").write_text(text)
        return text
