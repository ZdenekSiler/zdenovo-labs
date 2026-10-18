"""Write cassettes/series.json: HAND-WRITTEN answers in the documented response shape.

These are not model outputs. They exist so the demos exercise the routing, thresholds and
confidence code offline. With TYPESAFE_API_KEY set, demo.py records real responses over the
top of this file - do that before quoting any number as Jev's.

  uv run python make_fixtures.py
"""
from __future__ import annotations

from cassette import Cassette, _key
from confidence import Confidence
from demo import DATE_DOC, DOCS, ITEMS, ORDER_STATE, PICK_DOC, TICKETS, TOOL_CASES
from guard import DOCUMENT_BATTERY, TOOL_CALL_QUESTIONS
from jagged import DateParts
from triage import QUESTIONS, TicketTriage


def choice(probs: dict[str, float]) -> dict:
  return {"type": "choice", "choice": max(probs, key=probs.get), "probabilities": probs,
          "confidence": round(Confidence.choice(probs), 2)}


def score(probs: list[float], legend: list[str]) -> dict:
  p = {str(i): v for i, v in enumerate(probs)}
  return {"type": "score", "score": round(sum(i * v for i, v in enumerate(probs)), 2), "probabilities": p,
          "legend": {str(i): s for i, s in enumerate(legend)}, "confidence": round(Confidence.score(p), 2)}


def noul(p: float) -> dict:
  return {"type": "noul", "noul": p}


def response(answers: dict, tokens: int) -> dict:
  return {"model": "jev-1.13.0", "answers": answers, "usage": {"input_tokens": tokens, "output_tokens": 18 * len(answers)}}


SEV = [str(c) for c in QUESTIONS["bug_severity"].criteria]
FRU = [str(c) for c in QUESTIONS["frustration"].criteria]
CATS = ["bug_report", "billing", "feature_request", "other"]

TRIAGE_ANSWERS = [
  # export crash with steps
  dict(category=choice(dict(zip(CATS, [0.93, 0.03, 0.02, 0.02]))), bug_severity=score([0, 0.05, 0.55, 0.40], SEV),
       has_repro_steps=noul(0.94), refund_requested=noul(0.03), frustration=score([0.05, 0.7, 0.25], FRU)),
  # double charge + refund
  dict(category=choice(dict(zip(CATS, [0.06, 0.90, 0.01, 0.03]))), bug_severity=score([0.6, 0.3, 0.1, 0], SEV),
       has_repro_steps=noul(0.08), refund_requested=noul(0.96), frustration=score([0.1, 0.6, 0.3], FRU)),
  # ambiguous: "billing page shows wrong currency"
  dict(category=choice(dict(zip(CATS, [0.46, 0.41, 0.05, 0.08]))), bug_severity=score([0.5, 0.4, 0.1, 0], SEV),
       has_repro_steps=noul(0.31), refund_requested=noul(0.12), frustration=score([0.7, 0.3, 0], FRU)),
]


def main() -> None:
  tape = Cassette("series")
  tape.entries.clear()

  for ticket, answers in zip(TICKETS, TRIAGE_ANSWERS):
    body = {"state": TicketTriage(None).state(ticket), "questions": QUESTIONS}
    tape.entries[_key(body)] = response(answers, 410)

  opts = list(ORDER_STATE["criteria"])
  perm = [dict(zip(opts, ps)) for ps in ([0.52, 0.44, 0.04], [0.38, 0.58, 0.04], [0.47, 0.49, 0.04])]
  tape.entries[_key({"state": ORDER_STATE["state"], "questions": {f"perm_{i}": 0 for i in range(3)}})] = \
    response({f"perm_{i}": choice(p) for i, p in enumerate(perm)}, 260)

  dp = DateParts(None)
  months = [m for m in dp.questions("x")["month"].criteria]
  years = [y for y in dp.questions("x")["year"].criteria]
  days = [d for d in dp.questions("x")["day"].criteria]

  def peak(keys: list[str], winner: str, p: float) -> dict:
    rest = (1 - p) / (len(keys) - 1)
    return {k: (p if k == winner else round(rest, 4)) for k in keys}

  tape.entries[_key({"state": {"document": DATE_DOC}, "questions": {"year": 0, "month": 0, "day": 0}})] = response(
    {"year": choice(peak(years, "2026", 0.97)), "month": choice(peak(months, "november", 0.95)),
     "day": choice(peak(days, "3", 0.91))}, 520)

  fruit = {"apple", "banana", "orange"}
  tape.entries[_key({"state": {"items": ITEMS}, "questions": {f"item_{i}": 0 for i in range(len(ITEMS))}})] = response(
    {f"item_{i}": noul(0.97 if it in fruit else 0.03) for i, it in enumerate(ITEMS)}, 300)

  tape.entries[_key({"state": {"document": PICK_DOC}, "questions": {"pick": 0}})] = response(
    {"pick": choice({"c0": 0.91, "c1": 0.06, "none": 0.03})}, 240)

  doc_signals = {"release_notes": [0.02, 0.03, 0.01, 0.01], "poisoned_readme": [0.91, 0.88, 0.62, 0.79]}
  for name, text in DOCS.items():
    tape.entries[_key({"state": {"source": name, "document": text}, "questions": DOCUMENT_BATTERY})] = response(
      {k: noul(v) for k, v in zip(DOCUMENT_BATTERY, doc_signals[name])}, 380)

  tool_answers = [
    dict(decision=choice({"allow": 0.81, "confirm": 0.16, "deny": 0.03}), argument_from_untrusted=noul(0.04),
         irreversibility=score([0.0, 0.7, 0.3, 0.0], ["a", "b", "c", "d"])),
    dict(decision=choice({"allow": 0.07, "confirm": 0.21, "deny": 0.72}), argument_from_untrusted=noul(0.93),
         irreversibility=score([0, 0.1, 0.3, 0.6], ["a", "b", "c", "d"])),
  ]
  for (req, call, ctx), ans in zip(TOOL_CASES, tool_answers):
    state = {"user_request": req, "proposed_call": call, "untrusted_context": ctx}
    tape.entries[_key({"state": state, "questions": TOOL_CALL_QUESTIONS})] = response(ans, 450)

  tape.save()
  print(f"wrote {len(tape.entries)} hand-written entries to {tape.path}")


if __name__ == "__main__":
  main()
