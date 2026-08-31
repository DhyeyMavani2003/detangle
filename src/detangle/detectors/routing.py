"""Selection & routing detectors: DTS01/02/03 and DTP05.

Model-triggered activation (skill and subagent descriptions) is the
biggest genuinely-unserved ambiguity surface: no ecosystem documents
arbitration between overlapping triggers.
"""

from __future__ import annotations

from collections import defaultdict

from ..activation import description_overlap
from ..findings import Evidence, Finding
from ..ir import ActivationMode, SourceSpan
from ..lexicons import content_tokens
from ..taxonomy import Severity
from .base import AnalysisContext, Detector

_TRIGGER_OVERLAP_THRESHOLD = 0.45
_ROOT_MEMORY_FILES = (
    "CLAUDE.md",
    "AGENTS.md",
    "AGENT.md",
    ".cursorrules",
    ".github/copilot-instructions.md",
    ".rules",
)


class TriggerOverlapDetector(Detector):
    """DTS01: skill/rule descriptions competing for the same intents."""

    codes = ("DTS01",)
    name = "trigger-overlap"

    def run(self, ctx: AnalysisContext) -> list[Finding]:
        triggered = [
            cf
            for cf in ctx.corpus.files
            if cf.activation.mode == ActivationMode.MODEL and cf.activation.description.strip()
        ]
        out: list[Finding] = []
        for i in range(len(triggered)):
            for j in range(i + 1, len(triggered)):
                a, b = triggered[i], triggered[j]
                if a.mechanism != b.mechanism:
                    continue  # a skill and a subagent do not compete for one slot
                sim = description_overlap(a.activation.description, b.activation.description)
                if sim < _TRIGGER_OVERLAP_THRESHOLD:
                    continue
                name_a = a.meta.get("skill_name") or a.meta.get("agent_name") or a.path
                name_b = b.meta.get("skill_name") or b.meta.get("agent_name") or b.path
                shared = sorted(
                    set(content_tokens(a.activation.description))
                    & set(content_tokens(b.activation.description))
                )[:8]
                out.append(
                    Finding(
                        code="DTS01",
                        message=(
                            f"Trigger overlap between '{name_a}' and '{name_b}' "
                            f"({sim:.0%} description overlap): the model routes on these "
                            "descriptions and no ecosystem documents arbitration — "
                            "which one fires is nondeterministic."
                        ),
                        severity=Severity.WARNING,
                        evidence=[
                            Evidence(
                                SourceSpan(a.path, 1, 1),
                                a.activation.description[:160],
                                "trigger 1",
                            ),
                            Evidence(
                                SourceSpan(b.path, 1, 1),
                                b.activation.description[:160],
                                "trigger 2",
                            ),
                        ],
                        co_activation="both are description-triggered in the same context",
                        suggestion=(
                            "Differentiate the descriptions (say when to use THIS one, "
                            f"not the other) — shared terms: {', '.join(shared)}."
                        ),
                        confidence=min(1.0, 0.5 + sim),
                    )
                )
        return out


class ShadowedNameDetector(Detector):
    """DTS03: two mechanisms/levels claiming the same skill/agent name."""

    codes = ("DTS03",)
    name = "shadowed-name"

    def run(self, ctx: AnalysisContext) -> list[Finding]:
        by_name: dict[tuple[str, str], list] = defaultdict(list)
        for cf in ctx.corpus.files:
            name = cf.meta.get("skill_name") or cf.meta.get("agent_name")
            if name:
                by_name[(cf.mechanism, str(name).lower())].append(cf)
        out: list[Finding] = []
        for (mechanism, name), files in sorted(by_name.items()):
            if len(files) < 2:
                continue
            paths = [f.path for f in files]
            out.append(
                Finding(
                    code="DTS03",
                    message=(
                        f"Name collision: {len(files)} {mechanism}s named '{name}' "
                        f"({', '.join(paths)}). Name-shadowing rules will silently pick "
                        "one; the others never load under that name."
                    ),
                    severity=Severity.WARNING,
                    evidence=[Evidence(SourceSpan(f.path, 1, 1), f.path, mechanism) for f in files],
                    suggestion="Rename so each name maps to exactly one definition.",
                )
            )
        return out


class DescriptionMismatchDetector(Detector):
    """DTS02: description promises with no support in the body (conservative)."""

    codes = ("DTS02",)
    name = "description-mismatch"

    def run(self, ctx: AnalysisContext) -> list[Finding]:
        out: list[Finding] = []
        for cf in ctx.corpus.files:
            if cf.activation.mode != ActivationMode.MODEL:
                continue
            desc = cf.activation.description
            body = cf.text
            desc_tokens = set(content_tokens(desc))
            body_tokens = set(content_tokens(body))
            if len(desc_tokens) < 4 or len(body_tokens) < 30:
                continue
            overlap = len(desc_tokens & body_tokens) / len(desc_tokens)
            if overlap >= 0.12:
                continue
            name = cf.meta.get("skill_name") or cf.meta.get("agent_name") or cf.path
            out.append(
                Finding(
                    code="DTS02",
                    message=(
                        f"'{name}': the trigger description barely overlaps the body "
                        f"({overlap:.0%} of description terms appear in it) — the model "
                        "routes on promises the body may not deliver."
                    ),
                    severity=Severity.ADVISORY,
                    evidence=[Evidence(SourceSpan(cf.path, 1, 1), desc[:160], "description")],
                    suggestion="Align the description with what the body actually covers.",
                    confidence=0.6,
                )
            )
        return out


class DivergentInterpretationDetector(Detector):
    """DTP05: different tools would see materially different instruction sets."""

    codes = ("DTP05",)
    name = "divergent-interpretation"

    def run(self, ctx: AnalysisContext) -> list[Finding]:
        from ..similarity import text_similarity

        roots = [
            cf
            for cf in ctx.corpus.files
            if cf.path in _ROOT_MEMORY_FILES and cf.mechanism == "memory"
        ]
        if len(roots) < 2:
            return []
        out: list[Finding] = []
        for i in range(len(roots)):
            for j in range(i + 1, len(roots)):
                a, b = roots[i], roots[j]
                sim = text_similarity(a.text, b.text)
                if sim >= 0.55:
                    continue  # mirrored/synced content is fine
                ra = set(a.meta.get("readers", ()))
                rb = set(b.meta.get("readers", ()))
                only_a = sorted(ra - rb)
                only_b = sorted(rb - ra)
                if not only_a and not only_b:
                    continue
                msg = (
                    f"{a.path} and {b.path} differ materially (similarity {sim:.0%}), "
                    "so different tools see different instruction sets"
                )
                if only_a or only_b:
                    msg += (
                        ": "
                        + (f"only {', '.join(only_a)} read(s) {a.path}" if only_a else "")
                        + ("; " if only_a and only_b else "")
                        + (f"only {', '.join(only_b)} read(s) {b.path}" if only_b else "")
                    )
                out.append(
                    Finding(
                        code="DTP05",
                        message=msg + ".",
                        severity=Severity.ADVISORY,
                        evidence=[
                            Evidence(SourceSpan(a.path, 1, 1), a.path, ""),
                            Evidence(SourceSpan(b.path, 1, 1), b.path, ""),
                        ],
                        suggestion=(
                            "Mirror the shared rules across both files (or generate one "
                            "from the other) so every tool sees the same policy."
                        ),
                    )
                )
        # surface the Zed first-match note as evidence when present
        for note in ctx.corpus.notes:
            if note.startswith("Zed reads only"):
                out.append(
                    Finding(
                        code="DTP05",
                        message=note + ".",
                        severity=Severity.ADVISORY,
                        evidence=[],
                        suggestion="",
                        confidence=0.9,
                    )
                )
        return out
