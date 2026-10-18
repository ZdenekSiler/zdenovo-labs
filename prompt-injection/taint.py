"""A CaMeL-flavoured tool gate in ~100 lines: track where every value came from, and let
policy - not the model - decide whether a tool call may happen.

This is not CaMeL (arXiv 2503.18813). CaMeL has a privileged planner emit a program and a
custom interpreter propagate capabilities through every operation. This is the part you
can bolt onto an existing agent loop this afternoon: values carry provenance labels,
labels union when values combine, and each tool declares what its arguments may touch.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Source(str, Enum):
  USER = "user"  # typed by the person the agent works for
  SYSTEM = "system"  # your code / config
  WEB = "web"  # fetched pages, search results
  EMAIL = "email"  # inbound mail bodies
  TOOL = "tool"  # output of another tool (inherits nothing by default - be explicit)


TRUSTED = frozenset({Source.USER, Source.SYSTEM})


@dataclass(frozen=True)
class Value:
  data: str
  sources: frozenset[Source]

  @property
  def tainted(self) -> bool:
    return not self.sources <= TRUSTED

  def __add__(self, other: "Value") -> "Value":
    return Value(self.data + other.data, self.sources | other.sources)


def trusted(data: str, src: Source = Source.USER) -> Value:
  return Value(data, frozenset({src}))


def untrusted(data: str, src: Source) -> Value:
  return Value(data, frozenset({src}))


class Verdict(str, Enum):
  ALLOW = "allow"
  CONFIRM = "confirm"  # ask the human, show them the tainted argument verbatim
  DENY = "deny"


@dataclass(frozen=True)
class ToolSpec:
  name: str
  # Arguments that steer *where* data goes or *what* gets changed. Tainted values here are
  # the injection's goal: a recipient, a URL, a shell command, a file path.
  control_args: frozenset[str] = frozenset()
  # Does the tool send data outside the trust boundary or change external state?
  external: bool = False


@dataclass
class Decision:
  verdict: Verdict
  reason: str


@dataclass
class ToolGate:
  specs: dict[str, ToolSpec] = field(default_factory=dict)
  # Once the session has read untrusted content, may it still talk to the outside world?
  # (Meta's "Rule of Two": untrusted input + private data + external action - pick two.)
  session_has_private_data: bool = False

  def register(self, spec: ToolSpec) -> None:
    self.specs[spec.name] = spec

  def check(self, tool: str, args: dict[str, Value]) -> Decision:
    spec = self.specs.get(tool)
    if spec is None:
      return Decision(Verdict.DENY, f"unknown tool {tool!r}")
    for name in spec.control_args:
      v = args.get(name)
      if v is not None and v.tainted:
        srcs = ",".join(sorted(s.value for s in v.sources))
        return Decision(Verdict.DENY, f"control arg {name!r} derives from untrusted source ({srcs})")
    if spec.external:
      tainted_payload = [n for n, v in args.items() if v.tainted]
      if tainted_payload and self.session_has_private_data:
        return Decision(Verdict.CONFIRM, f"external call carries untrusted data in {tainted_payload} "
                                         "in a session holding private data")
    return Decision(Verdict.ALLOW, "no tainted control flow")
