"""Part 5: workarounds for the documented jev-1.13 failure modes.

Every class here follows the same rule from TypeSafe's jaggedness page: keep judgment in
the model, keep arithmetic, ordering and counting in code.
"""
from __future__ import annotations

import calendar
import random
import re
from dataclasses import dataclass
from datetime import date

from typesafe_sdk import Choice, Noul, TypeSafeClient

NOT_STATED = "not_stated"


class OptionOrderProbe:
  """Failure mode 8: Choice leans toward the first option. Ask the same Choice with the
  options shuffled, in ONE request (questions are independent, so this is a free re-roll),
  and report whether the winners agree."""

  def __init__(self, client: TypeSafeClient, shuffles: int = 3, seed: int = 7) -> None:
    self.client = client
    self.shuffles = shuffles
    self.rng = random.Random(seed)

  def ask(self, state, instructions: str, criteria: dict) -> dict:
    keys = list(criteria)
    questions = {}
    for i in range(self.shuffles):
      order = keys[:] if i == 0 else self.rng.sample(keys, len(keys))
      questions[f"perm_{i}"] = Choice(instructions=instructions, criteria={k: criteria[k] for k in order})
    r = self.client.system_one(state, questions)
    winners = [r.choices[q].choice for q in questions]
    # Average the distributions: a cheap debias when the permutations disagree.
    mean = {k: sum(r.choices[q].probabilities[k] for q in questions) / len(questions) for k in keys}
    return {"winners": winners, "consistent": len(set(winners)) == 1,
            "choice": max(mean, key=mean.get), "mean_probabilities": mean}


class DateParts:
  """Failure mode 3: Jev reads dates as text. Extract the parts as closed-set Choices with
  an explicit 'not stated' option, then build and compare the date in code."""

  def __init__(self, client: TypeSafeClient, years: range = range(2020, 2031)) -> None:
    self.client = client
    self.years = years

  def questions(self, what: str) -> dict:
    months = {calendar.month_name[m].lower(): None for m in range(1, 13)}
    return {
      "year": Choice(instructions=f"In which year is {what}?",
                     criteria={str(y): None for y in self.years} | {NOT_STATED: "The text gives no year"}),
      "month": Choice(instructions=f"In which month is {what}?",
                      criteria=months | {NOT_STATED: "The text gives no month"}),
      "day": Choice(instructions=f"On which day of the month is {what}?",
                    criteria={str(d): None for d in range(1, 32)} | {NOT_STATED: "The text gives no day"}),
    }

  def extract(self, text: str, what: str, min_confidence: float = 0.6) -> date | None:
    r = self.client.system_one({"document": text}, self.questions(what))
    parts = {k: r.choices[k] for k in ("year", "month", "day")}
    if any(a.choice == NOT_STATED or a.confidence < min_confidence for a in parts.values()):
      return None  # route to a human; don't guess a date
    month = list(calendar.month_name).index(parts["month"].choice.capitalize())
    try:
      return date(int(parts["year"].choice), month, int(parts["day"].choice))
    except ValueError:  # 31 February: the model picked parts that don't make a date
      return None


class CountInCode:
  """Failure mode 2: never ask 'how many'. One Noul per candidate, sum in code."""

  def __init__(self, client: TypeSafeClient, threshold: float = 0.5) -> None:
    self.client = client
    self.threshold = threshold

  def count(self, items: list[str], predicate: str) -> tuple[int, list[str]]:
    questions = {f"item_{i}": Noul(instructions=f"Is `items[{i}]` {predicate}?") for i in range(len(items))}
    r = self.client.system_one({"items": items}, questions)
    hits = [items[i] for i in range(len(items)) if r.nouls[f"item_{i}"].noul > self.threshold]
    return len(hits), hits


@dataclass(frozen=True)
class Candidate:
  key: str
  value: str


class CandidateSelector:
  """Failure mode 9: Jev doesn't generate. Find candidate spans with a regex, let Jev pick
  which one answers the question, copy the value verbatim in code."""

  PATTERNS = {
    "email": re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"),  # no trailing "." from sentence ends
    "amount": re.compile(r"(?:USD|EUR|\$|€)\s?\d[\d,]*(?:\.\d{2})?"),
  }

  def __init__(self, client: TypeSafeClient) -> None:
    self.client = client

  def candidates(self, text: str, kind: str) -> list[Candidate]:
    seen: dict[str, Candidate] = {}
    for m in self.PATTERNS[kind].finditer(text):
      seen.setdefault(m.group(0), Candidate(f"c{len(seen)}", m.group(0)))
    return list(seen.values())

  def pick(self, text: str, kind: str, question: str) -> str | None:
    cands = self.candidates(text, kind)
    if not cands:
      return None
    criteria = {c.key: c.value for c in cands} | {"none": "None of the candidates answers the question"}
    r = self.client.system_one({"document": text},
                               {"pick": Choice(instructions=question, criteria=criteria)})
    winner = r.choices["pick"].choice
    return next((c.value for c in cands if c.key == winner), None)
