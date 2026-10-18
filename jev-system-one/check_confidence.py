"""Recompute the confidence values printed in TypeSafe's own docs from their probabilities.

  uv run python check_confidence.py
"""
from confidence import Confidence

DOC_EXAMPLES = [
  # (where in the docs, kind, probabilities, confidence the docs print)
  ("quickstart: department", "choice", {"technical": 0.85, "sales": 0.0, "billing": 0.15}, 0.78),
  ("api ref: department", "choice", {"billing": 0.88, "technical": 0.12, "sales": 0.0}, 0.81),
  ("api ref: frustration", "score", {"0": 0.0, "1": 0.95, "2": 0.05}, 0.92),
  ("confidence page: bug_severity", "score", {"0": 0.0, "1": 0.57, "2": 0.43}, 0.35),
  ("confidence page: adjacent tie", "score", {"0": 0.0, "1": 0.5, "2": 0.5}, 0.25),
  ("confidence page: opposite tie", "score", {"0": 0.5, "1": 0.0, "2": 0.5}, 0.0),
  ("confidence page: (.6,.3,.1)", "choice", {"a": 0.6, "b": 0.3, "c": 0.1}, 0.4),
  ("confidence page: (.6,.2,.2)", "choice", {"a": 0.6, "b": 0.2, "c": 0.2}, 0.4),
]

print(f"{'example':<32}{'docs':>6}{'ours':>8}{'top2':>8}")
for label, kind, probs, documented in DOC_EXAMPLES:
  ours = Confidence.choice(probs) if kind == "choice" else Confidence.score(probs)
  ratio = Confidence.top2_ratio(probs)
  flag = "" if abs(ours - documented) < 0.011 else "  <-- differs"
  print(f"{label:<32}{documented:>6.2f}{ours:>8.3f}{ratio:>8.2f}{flag}")

print("\nnoul as confidence:", {p: round(Confidence.noul(p), 2) for p in (0.02, 0.35, 0.5, 0.74, 0.98)})
