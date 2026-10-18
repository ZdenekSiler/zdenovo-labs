"""Five-minute agent audit: Willison's lethal trifecta / Meta's Agents Rule of Two.

Tag each tool with what it gives the agent. If one session can reach all three legs, an
injected instruction can complete an exfiltration on its own - no bug required.
"""
from __future__ import annotations

from dataclasses import dataclass, field

UNTRUSTED = "untrusted_input"
PRIVATE = "private_data"
EGRESS = "external_comms"
LEGS = (UNTRUSTED, PRIVATE, EGRESS)


@dataclass(frozen=True)
class Tool:
  name: str
  legs: frozenset[str]
  note: str = ""


@dataclass
class AgentProfile:
  name: str
  tools: list[Tool] = field(default_factory=list)
  # Channels that exist without any tool: a chat UI that renders Markdown images is egress.
  implicit: frozenset[str] = frozenset()

  def legs(self) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {leg: [] for leg in LEGS}
    for t in self.tools:
      for leg in t.legs:
        out[leg].append(t.name)
    for leg in self.implicit:
      out[leg].append("(implicit)")
    return out

  def audit(self) -> str:
    legs = self.legs()
    present = [leg for leg in LEGS if legs[leg]]
    lines = [f"agent: {self.name}"]
    for leg in LEGS:
      mark = "x" if legs[leg] else " "
      lines.append(f"  [{mark}] {leg:<16} {', '.join(legs[leg]) or '-'}")
    if len(present) == 3:
      lines.append("  VERDICT: lethal trifecta - an injection can exfiltrate without any bug")
    else:
      missing = [leg for leg in LEGS if leg not in present]
      lines.append(f"  VERDICT: {len(present)}/3 legs - keep it that way (missing: {', '.join(missing)})")
    return "\n".join(lines)
