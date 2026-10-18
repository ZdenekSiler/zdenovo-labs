"""Confidence maths, reimplemented from docs.typesafe.ai/confidence so you can (a) check the
API's numbers and (b) compute alternatives the API doesn't return.
"""
from __future__ import annotations

from collections.abc import Mapping

from typesafe_sdk import ChoiceAnswer, NoulAnswer, ScoreAnswer


class Confidence:
  @staticmethod
  def noul(p: float) -> float:
    """Distance from a coin flip. Same scale as a 2-option Choice."""
    return abs(2 * p - 1)

  @staticmethod
  def choice(probabilities: Mapping[str, float]) -> float:
    ps = list(probabilities.values())
    n = len(ps)
    return (max(ps) - 1 / n) / (1 - 1 / n)

  @staticmethod
  def score(probabilities: Mapping[str, float]) -> float:
    ps = [probabilities[k] for k in sorted(probabilities, key=int)]
    n = len(ps)
    m = ps.index(max(ps))
    spread = sum(p * abs(i - m) for i, p in enumerate(ps))
    even_spread = sum(abs(i - (n - 1) / 2) for i in range(n)) / n
    return max(0.0, 1 - spread / even_spread)

  @staticmethod
  def top2_ratio(probabilities: Mapping[str, float]) -> float:
    """How clearly the winner beats the runner-up. inf when the runner-up is 0."""
    first, second = sorted(probabilities.values(), reverse=True)[:2]
    return float("inf") if second == 0 else first / second

  @classmethod
  def of(cls, answer: NoulAnswer | ChoiceAnswer | ScoreAnswer) -> float:
    """One gate for every answer type."""
    if isinstance(answer, NoulAnswer):
      return cls.noul(answer.noul)
    return answer.confidence
