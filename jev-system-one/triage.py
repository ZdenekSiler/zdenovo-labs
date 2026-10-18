"""Part 4: one request, many questions, routing in code.

Speculative fan-out + composite scoring + confidence-gated routing in one class. The
questions are data (QUESTIONS), the policy is data (Policy), and the routing is plain
Python you can unit-test without a network.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from typesafe_sdk import Choice, Noul, NoulCriteria, Score, SystemOneResponse, TypeSafeClient


class Route(str, Enum):
  HUMAN = "human"
  ENGINEERING_P1 = "engineering_p1"
  BUG_BACKLOG = "bug_backlog"
  BILLING_REFUND = "billing_refund"
  BILLING = "billing"
  FEATURE_LOG = "feature_log"


QUESTIONS = {
  "category": Choice(
    instructions="What is the main purpose of `ticket.message`?",
    criteria={
      "bug_report": {"what": "Something in the product is broken or behaves wrongly",
                     "not_for": "Questions about charges or requests for new features"},
      "billing": {"what": "Charges, invoices, refunds or subscriptions",
                  "not_for": "Product defects, even if they cost the customer money"},
      "feature_request": {"what": "Asks for a capability the product does not have",
                          "not_for": "Reports that an existing capability is broken"},
      "other": None,
    },
  ),
  # Speculative: only meaningful for bug reports. Asked anyway - it's nearly free.
  "bug_severity": Score(
    instructions="If `ticket.message` reports a defect, how severe is its impact?",
    criteria=[
      "Cosmetic: no effect on getting work done",
      "Degraded: a workaround exists",
      "Blocking: a core workflow cannot be completed",
      "Critical: data loss, security exposure, or money lost",
    ],
  ),
  "has_repro_steps": Noul(
    instructions="Does `ticket.message` describe the steps that trigger the problem?",
  ),
  # Speculative: only meaningful for billing.
  "refund_requested": Noul(
    instructions="Does `ticket.message` explicitly ask for money back or a credit?",
    criteria=NoulCriteria(
      true="Directly asks for a refund or account credit",
      false="Complains or asks about a charge without requesting a remedy",
    ),
  ),
  "frustration": Score(
    instructions="How frustrated does the customer appear in `ticket.message`?",
    criteria=["Calm, stating facts", "Frustrated but civil", "Very angry, strong language"],
  ),
}


@dataclass(frozen=True)
class Policy:
  min_category_confidence: float = 0.5
  p1_severity: float = 2.0  # on the 0-3 scale above
  p1_repro: float = 0.6
  refund_flag: float = 0.7
  priority_frustration: float = 1.5  # on the 0-2 scale above


@dataclass(frozen=True)
class Decision:
  route: Route
  priority: bool
  why: str
  model: str
  request_id: str | None


class TicketTriage:
  def __init__(self, client: TypeSafeClient, policy: Policy = Policy()) -> None:
    self.client = client
    self.policy = policy

  def state(self, ticket: dict) -> dict:
    # Send only what the questions read. Jev rots on irrelevant state like any model does.
    return {"ticket": {"subject": ticket["subject"], "message": ticket["message"]}}

  def assess(self, ticket: dict) -> SystemOneResponse:
    return self.client.system_one(self.state(ticket), QUESTIONS)

  def route(self, r: SystemOneResponse) -> Decision:
    p = self.policy
    category = r.choices["category"]
    severity = r.scores["bug_severity"]
    repro = r.nouls["has_repro_steps"]
    refund = r.nouls["refund_requested"]
    priority = r.scores["frustration"].score > p.priority_frustration

    def decide(route: Route, why: str) -> Decision:
      return Decision(route, priority, why, r.model, r.request_id)

    if category.confidence < p.min_category_confidence:
      return decide(Route.HUMAN, f"category unclear ({category.choice} @ {category.confidence:.2f})")
    if category.choice == "bug_report":
      if severity.score >= p.p1_severity and repro.noul > p.p1_repro:
        return decide(Route.ENGINEERING_P1, f"severity {severity.score:.2f}, repro {repro.noul:.2f}")
      return decide(Route.BUG_BACKLOG, f"severity {severity.score:.2f}, repro {repro.noul:.2f}")
    if category.choice == "billing":
      if refund.noul > p.refund_flag:
        return decide(Route.BILLING_REFUND, f"refund {refund.noul:.2f}")
      return decide(Route.BILLING, f"refund {refund.noul:.2f}")
    if category.choice == "feature_request":
      return decide(Route.FEATURE_LOG, "feature request")
    return decide(Route.HUMAN, "category 'other'")

  def __call__(self, ticket: dict) -> Decision:
    return self.route(self.assess(ticket))
