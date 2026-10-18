"""Invisible-text smuggling: encode, decode and detect.

The Unicode Tags block (U+E0000-U+E007F) shadows printable ASCII one-to-one and renders
as nothing in most UIs. Tokenizers still see it. That makes it a covert channel in both
directions: instructions hidden *in* to the model, and data hidden *out* of it.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass

TAG_BASE = 0xE0000


class TagSmuggler:
  """Round-trips ASCII through the Unicode Tags block."""

  def encode(self, text: str) -> str:
    return "".join(chr(TAG_BASE + ord(c)) for c in text if 0x20 <= ord(c) < 0x7F)

  def decode(self, text: str) -> str:
    return "".join(chr(ord(c) - TAG_BASE) for c in text if TAG_BASE + 0x20 <= ord(c) < TAG_BASE + 0x7F)


@dataclass(frozen=True)
class Finding:
  kind: str
  count: int
  sample: str


class InvisibleCharScanner:
  """Flags characters a human reviewer cannot see but a model can read."""

  RANGES = {
    "unicode_tags": [(0xE0000, 0xE007F)],
    "zero_width": [(0x200B, 0x200D), (0x2060, 0x2064), (0xFEFF, 0xFEFF)],
    "bidi_controls": [(0x202A, 0x202E), (0x2066, 0x2069)],
    "variation_selectors": [(0xFE00, 0xFE0F), (0xE0100, 0xE01EF)],
  }

  def __init__(self) -> None:
    self._smuggler = TagSmuggler()

  def _kind(self, ch: str) -> str | None:
    cp = ord(ch)
    for kind, ranges in self.RANGES.items():
      if any(lo <= cp <= hi for lo, hi in ranges):
        return kind
    return None

  def scan(self, text: str) -> list[Finding]:
    buckets: dict[str, list[str]] = {}
    for ch in text:
      kind = self._kind(ch)
      if kind:
        buckets.setdefault(kind, []).append(ch)
    findings = []
    for kind, chars in buckets.items():
      sample = self._smuggler.decode("".join(chars)) if kind == "unicode_tags" else f"U+{ord(chars[0]):04X}"
      findings.append(Finding(kind, len(chars), sample[:60]))
    return findings

  def strip(self, text: str) -> str:
    """Remove every flagged character, then NFKC-normalise what's left."""
    return unicodedata.normalize("NFKC", "".join(ch for ch in text if self._kind(ch) is None))
