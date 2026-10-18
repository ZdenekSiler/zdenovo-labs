"""Part 3: 'calibrated' is a claim about groups of predictions. Measure it on yours.

Feed it (probability, was_it_actually_true) pairs from a labelled sample of your own
traffic and it tells you how far the probabilities are from frequencies, and what a
threshold buys you in coverage vs accuracy.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Bin:
  lo: float
  hi: float
  n: int
  mean_p: float
  frequency: float


class ReliabilityReport:
  def __init__(self, pairs: list[tuple[float, bool]], bins: int = 5) -> None:
    if not pairs:
      raise ValueError("need at least one (probability, outcome) pair")
    self.pairs = pairs
    self.bins = bins

  def table(self) -> list[Bin]:
    out = []
    for b in range(self.bins):
      lo, hi = b / self.bins, (b + 1) / self.bins
      members = [(p, y) for p, y in self.pairs if lo <= p < hi or (b == self.bins - 1 and p == 1.0)]
      if members:
        out.append(Bin(lo, hi, len(members), sum(p for p, _ in members) / len(members),
                       sum(y for _, y in members) / len(members)))
    return out

  def ece(self) -> float:
    """Expected calibration error: bin-size-weighted |mean probability - observed rate|."""
    n = len(self.pairs)
    return sum(b.n / n * abs(b.mean_p - b.frequency) for b in self.table())

  def sweep(self, thresholds: list[float]) -> list[tuple[float, float, float]]:
    """For 'act when p >= t, else escalate': (t, coverage, precision of the acted-on)."""
    rows = []
    for t in thresholds:
      acted = [y for p, y in self.pairs if p >= t]
      coverage = len(acted) / len(self.pairs)
      precision = sum(acted) / len(acted) if acted else float("nan")
      rows.append((t, coverage, precision))
    return rows

  def render(self) -> str:
    lines = [f"{'bin':<11}{'n':>5}{'mean p':>9}{'observed':>10}"]
    for b in self.table():
      lines.append(f"[{b.lo:.1f},{b.hi:.1f}){b.n:>5}{b.mean_p:>9.2f}{b.frequency:>10.2f}")
    lines.append(f"ECE = {self.ece():.3f}")
    return "\n".join(lines)
