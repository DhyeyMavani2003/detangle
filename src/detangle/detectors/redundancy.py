"""Redundancy & drift detectors: DTR01 duplicate, DTR02 drift, DTR03 terms.

Duplicate vs conflict discipline (HANS lesson): high lexical overlap must
never imply conflict — the ConflictRouter runs first and claims disagreeing
pairs; whatever reaches this detector with high similarity and NO
disagreement is redundancy territory.
"""

from __future__ import annotations

from collections import defaultdict

from ..findings import Evidence, Finding, pair_evidence
from ..similarity import text_similarity, token_set
from ..taxonomy import Severity
from .base import AnalysisContext, Detector

_DUP_THRESHOLD = 0.90  # near-verbatim
_DRIFT_LOW = 0.62  # paraphrase band lower bound
_DRIFT_HIGH = 0.90


class DuplicateDetector(Detector):
    codes = ("DTR01", "DTR02")
    name = "duplicates"

    def run(self, ctx: AnalysisContext) -> list[Finding]:
        out: list[Finding] = []
        for pair in ctx.pairs:
            if ctx.is_claimed(pair):
                continue
            a, b = pair.a, pair.b
            if not a.is_instruction or not b.is_instruction:
                continue
            norm_equal = a.normalized.strip().lower() == b.normalized.strip().lower()
            sim = text_similarity(a.normalized, b.normalized)
            if not norm_equal and sim < _DRIFT_LOW:
                continue

            if norm_equal or sim >= _DUP_THRESHOLD:
                ctx.claim(pair)
                same_file = a.file.path == b.file.path
                out.append(
                    Finding(
                        code="DTR01",
                        message=(
                            "Duplicate instruction"
                            + (" within the same file" if same_file else " across files")
                            + ": the same prescription is stated twice — harmless today, "
                            "but the copies can silently diverge on the next edit."
                        ),
                        severity=Severity.ADVISORY,
                        evidence=pair_evidence(pair),
                        units=[a, b],
                        co_activation=pair.co_activation_account,
                        precedence=pair.precedence.account,
                        suggestion="Keep one copy; if both layers need it, reference rather than restate.",
                    )
                )
                continue

            # paraphrase band: same intent wording drifting apart. Require the
            # frames to still agree (a disagreement would have been claimed by
            # the ConflictRouter) and a shared action, to avoid flagging
            # sentences that merely share a topic.
            if a.frame.action and a.frame.action == b.frame.action and _material_word_diff(a, b):
                ctx.claim(pair)
                out.append(
                    Finding(
                        code="DTR02",
                        message=(
                            "Near-duplicate drift: these read as two versions of the "
                            "same instruction that have started to diverge — a merge "
                            "conflict in slow motion."
                        ),
                        severity=Severity.WARNING,
                        evidence=pair_evidence(pair),
                        units=[a, b],
                        co_activation=pair.co_activation_account,
                        precedence=pair.precedence.account,
                        suggestion=(
                            "Consolidate into one instruction (keeping the details both "
                            "copies carry), or make the difference explicit."
                        ),
                        confidence=0.8,
                    )
                )
        return out


def _material_word_diff(a, b) -> bool:
    """The two texts differ by at least one content word (not just phrasing)."""
    ta, tb = token_set(a.text), token_set(b.text)
    return bool(ta ^ tb)


class TerminologyDetector(Detector):
    """DTR03: the same term defined differently in different places."""

    codes = ("DTR03",)
    name = "terminology"

    def run(self, ctx: AnalysisContext) -> list[Finding]:
        by_term: dict[str, list] = defaultdict(list)
        for u in ctx.units:
            for t in u.defined_terms:
                by_term[t].append(u)
        out: list[Finding] = []
        for term, units in sorted(by_term.items()):
            if len(units) < 2:
                continue
            # compare each pair of definitions for material difference
            for i in range(len(units)):
                for j in range(i + 1, len(units)):
                    ua, ub = units[i], units[j]
                    if ua.text.strip().lower() == ub.text.strip().lower():
                        continue
                    if text_similarity(ua.text, ub.text) >= 0.85:
                        continue
                    out.append(
                        Finding(
                            code="DTR03",
                            message=(
                                f"The term '{term}' is defined in two places with "
                                "materially different wording."
                            ),
                            severity=Severity.ADVISORY,
                            evidence=[
                                Evidence(ua.span, ua.text, "definition 1"),
                                Evidence(ub.span, ub.text, "definition 2"),
                            ],
                            units=[ua, ub],
                            suggestion="Define the term once and reference it elsewhere.",
                            confidence=0.7,
                        )
                    )
        return out
