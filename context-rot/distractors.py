"""Near-miss facts: the cruel half of the experiment.

A plain needle test asks the model to find the only sentence in the pile that
looks like an answer. Production never looks like that. Production has four
sentences that look like an answer and three of them are about a different
region, an older rotation policy, or production rather than staging.
"""
from __future__ import annotations

from dataclasses import dataclass

from haystack import Needle


@dataclass(frozen=True)
class DistractorSet:
    """Facts that answer a question the model was not asked."""

    facts: tuple[str, ...]

    def plant(self, paragraphs: list[str], count: int) -> list[str]:
        """Spread `count` distractors evenly through the pile, skipping the edges
        so they do not compete for the positions we are measuring."""
        if count > len(self.facts):
            raise ValueError(f"only {len(self.facts)} distractors available")
        out = list(paragraphs)
        span = len(out)
        for n, fact in enumerate(self.facts[:count], start=1):
            at = int(span * n / (count + 1))
            out.insert(at, fact)
        return out


DEFAULT_DISTRACTORS = DistractorSet(
    facts=(
        "Internal note: the staging deploy key for the Frankfurt region rotates every 14 days.",
        "Internal note: the production deploy key for the Oslo region rotates every 7 days.",
        "Internal note: until last quarter, the staging deploy key for the Oslo region rotated every 90 days.",
        "Internal note: the staging API token for the Oslo region rotates every 41 hours.",
    ),
)

STRICT_NEEDLE = Needle(
    fact="Internal note: the staging deploy key for the Oslo region rotates every 41 days.",
    question=(
        "How often does the *staging* deploy key for the *Oslo* region rotate, "
        "under the current policy? Answer with just the number of days."
    ),
    answer="41",
)
