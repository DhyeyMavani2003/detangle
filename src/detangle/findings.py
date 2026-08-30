"""Findings: what detectors emit and reporters render.

A finding is a *smell with evidence*, not an accusation: it carries the
quoted spans, the co-activation account ("both load at launch"), the
precedence account ("no declared order; Cursor would apply team-first"),
a suggested resolution, and which analysis lanes agreed.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from .ir import InstructionUnit, SourceSpan, UnitPair
from .taxonomy import RULES, Severity


@dataclass
class Evidence:
    span: SourceSpan
    quote: str
    note: str = ""  # e.g. "says at most 3", "trigger description"


@dataclass
class Finding:
    code: str  # taxonomy code, e.g. "DTC01"
    message: str  # one-sentence headline
    severity: Severity
    evidence: list[Evidence] = field(default_factory=list)
    units: list[InstructionUnit] = field(default_factory=list)
    co_activation: str = ""  # "both load at launch" / "co-fire when editing src/api/**"
    precedence: str = ""  # "no declared order between .claude/rules files"
    suggestion: str = ""  # scope one unit, add precedence, merge, delete...
    witness: str = ""  # English witness scenario for conditional conflicts
    confidence: float = 1.0  # 0..1; deterministic lanes emit 1.0
    lanes: tuple[str, ...] = ("deterministic",)
    tags: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        r = RULES.get(self.code)
        return r.name if r else self.code

    @property
    def fingerprint(self) -> str:
        """Stable id for suppression files, SARIF partialFingerprints, dedup."""
        parts = [self.code] + sorted(u.uid for u in self.units)
        if not self.units:
            parts += [f"{e.span.path}:{e.span.start_line}" for e in self.evidence]
        h = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:12]
        return f"{self.code}:{h}"

    @property
    def primary_span(self) -> SourceSpan | None:
        if self.evidence:
            return self.evidence[0].span
        if self.units:
            return self.units[0].span
        return None


def pair_evidence(pair: UnitPair, note_a: str = "", note_b: str = "") -> list[Evidence]:
    """Standard two-span evidence for a pairwise finding."""
    return [
        Evidence(pair.a.span, pair.a.text, note_a),
        Evidence(pair.b.span, pair.b.text, note_b),
    ]


def dedupe(findings: list[Finding]) -> list[Finding]:
    """Drop exact duplicates (same fingerprint), keeping the worst severity."""
    best: dict[str, Finding] = {}
    order: list[str] = []
    for f in findings:
        fp = f.fingerprint
        if fp not in best:
            best[fp] = f
            order.append(fp)
        elif f.severity > best[fp].severity:
            best[fp] = f
    return [best[fp] for fp in order]
