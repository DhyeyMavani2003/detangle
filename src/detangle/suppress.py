"""Suppression pragmas: auditable, justification-required.

Syntax (inside any scanned config file, as an HTML comment):

    <!-- detangle-ignore DTC01: we intentionally keep both until Q3 -->
    <!-- detangle-ignore-file DTR05: examples reference planned files -->

- ``detangle-ignore`` suppresses findings of that code whose evidence
  touches the lines from the pragma to the end of the next non-comment
  block (practically: the instruction(s) right below it).
- ``detangle-ignore-file`` suppresses the code for the whole file.
- A pragma without a justification (no ``: reason``) is itself surfaced
  as a note — suppressions must say why.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .findings import Finding
from .ingest.base import Corpus
from .markdown import find_comments

_PRAGMA_RE = re.compile(
    r"^\s*detangle-(?P<scope>ignore|ignore-file)\s+"
    r"(?P<codes>DT[CPRSX]\d\d(?:\s*,\s*DT[CPRSX]\d\d)*)"
    r"\s*(?::\s*(?P<reason>.+?))?\s*$",
    re.IGNORECASE | re.DOTALL,
)

_NEARBY_LINES = 6  # a line pragma covers this many lines below it


@dataclass
class Suppression:
    path: str
    code: str
    line: int
    file_wide: bool
    reason: str

    def covers(self, finding: Finding) -> bool:
        if finding.code.upper() != self.code:
            return False
        for ev in finding.evidence:
            if ev.span.path != self.path:
                continue
            if self.file_wide:
                return True
            if self.line <= ev.span.start_line <= self.line + _NEARBY_LINES:
                return True
        return False


def collect_suppressions(corpus: Corpus) -> tuple[list[Suppression], list[str]]:
    """Scan all config files for pragmas. Returns (suppressions, warnings)."""
    sups: list[Suppression] = []
    warnings: list[str] = []
    for cf in corpus.files:
        # cf.text is the frontmatter-stripped body for rules/skills/cursor/
        # copilot files, but finding evidence lines are file-absolute —
        # offset pragma lines by body_start so covers() compares like units.
        offset = int(cf.meta.get("body_start", 1)) - 1
        for body, start, _end in find_comments(cf.text):
            m = _PRAGMA_RE.match(body)
            if not m:
                continue
            line = start + offset
            reason = (m.group("reason") or "").strip()
            codes = [c.strip().upper() for c in m.group("codes").split(",")]
            if not reason:
                warnings.append(
                    f"{cf.path}:{line}: suppression for {', '.join(codes)} has no "
                    "justification — add one (`detangle-ignore CODE: reason`)"
                )
            for code in codes:
                sups.append(
                    Suppression(
                        path=cf.path,
                        code=code,
                        line=line,
                        file_wide=m.group("scope").lower() == "ignore-file",
                        reason=reason,
                    )
                )
    return sups, warnings


def apply_suppressions(
    findings: list[Finding], sups: list[Suppression]
) -> tuple[list[Finding], list[tuple[Finding, Suppression]]]:
    """Split findings into (kept, suppressed-with-why)."""
    kept: list[Finding] = []
    suppressed: list[tuple[Finding, Suppression]] = []
    for f in findings:
        hit = next((s for s in sups if s.covers(f)), None)
        if hit is None:
            kept.append(f)
        else:
            suppressed.append((f, hit))
    return kept, suppressed
