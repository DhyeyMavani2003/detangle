"""Pairwise conflict detectors: the router from disagreement to taxonomy code.

The routing table (the one-sentence conflict formula factored into code):

  disagreement? ──no──> not our problem
      │yes
      ├─ numeric clash ────────────────────────────> DTC03
      ├─ cross-mechanism pair (precedence UNDOCUMENTED) ──> DTP04
      ├─ FORBID(precedence winner) vs PERMIT(loser) ──> DTX02
      ├─ precedence RESOLVED ──> no finding (declared hierarchy resolves it)
      ├─ scopes equal / both always-on:
      │     conditions differ ──> DTC02 (+witness)
      │     permit involved ──> DTC05
      │     soft-only ──> DTC08 (advisory)
      │     else ──> DTC01
      ├─ scope subset:
      │     winner covers loser fully ──> DTP01 (shadowed)
      │     narrow exception survives positionally ──> DTP03 (fragile)
      └─ partial scope overlap, no declared order ──> DTP02
"""

from __future__ import annotations

import re

from ..activation import EXPOSURE, scope_relation
from ..extract import has_exception_marker
from ..findings import Finding, pair_evidence
from ..ir import (
    ActivationMode,
    CoActiveClass,
    InstructionUnit,
    Modality,
    PrecedenceKind,
    UnitPair,
)
from ..lexicons import FORMAT_EXCLUSIVE_RE, FORMAT_TOKENS
from ..taxonomy import Severity
from .base import AnalysisContext, Detector

# the witness template supplies its own "When …", so a subordinator already
# carried by the condition text would double up ("When when working …")
_LEADING_SUBORDINATOR_RE = re.compile(r"^(?:when(?:ever)?|if|while)\s+", re.IGNORECASE)


def _witness(pair: UnitPair) -> str:
    """Synthesize the boundary-condition scenario in English (van Lamsweerde)."""
    conds: list[str] = []
    for u in (pair.a, pair.b):
        if u.frame.condition:
            cond = u.frame.condition.lower().rstrip(".")
            conds.append(_LEADING_SUBORDINATOR_RE.sub("", cond) or cond)
        elif u.activation.mode == ActivationMode.PATH and u.activation.globs:
            conds.append(f"working under {', '.join(u.activation.globs)}")
        elif u.activation.mode == ActivationMode.MODEL:
            desc = " ".join(u.activation.description.split()[:12])
            if desc:
                conds.append(f"'{desc}…' triggers")
    if not conds:
        return ""
    if len(conds) == 1:
        return f"When {conds[0]}, both instructions apply and cannot be jointly satisfied."
    return (
        f"When {conds[0]} and, at the same time, {conds[1]}, both instructions "
        "apply and cannot be jointly satisfied."
    )


def _suggest(pair: UnitPair, code: str) -> str:
    a, b = pair.a, pair.b
    same_file = a.file.path == b.file.path
    if code == "DTC03":
        return "Pick one limit and delete the other, or scope each to the situation it belongs to."
    if code in {"DTC01", "DTC05"}:
        if same_file:
            return "Merge the two into one instruction, or scope each with an explicit condition."
        return (
            "Delete one, or add an explicit precedence note (e.g. front-matter "
            "`overrides:`) so the intended winner is declared."
        )
    if code == "DTC02":
        return "Add a tie-breaker for the boundary case (e.g. 'X wins when both apply')."
    if code == "DTP01":
        return "Delete the shadowed instruction or narrow the higher-precedence one."
    if code == "DTP02":
        return "Declare precedence for the overlap, or make the scopes disjoint."
    if code == "DTP03":
        return "Mark the exception explicitly (e.g. 'Exception to the rule above:') so a reorder cannot silently break it."
    if code == "DTP04":
        return "Move both prescriptions into the same layer, or state in each which one yields."
    if code == "DTX02":
        return "Remove the broader grant from the lower-precedence file, or narrow it to match the restriction."
    if code == "DTC08":
        return "If both preferences are intended, state the trade-off explicitly (e.g. 'prefer X, but Y when Z')."
    return ""


class ConflictRouter(Detector):
    codes = (
        "DTC01",
        "DTC02",
        "DTC03",
        "DTC05",
        "DTC08",
        "DTP01",
        "DTP02",
        "DTP03",
        "DTP04",
        "DTX02",
    )
    name = "conflict-router"

    def run(self, ctx: AnalysisContext) -> list[Finding]:
        from .disagreement import find_disagreement

        out: list[Finding] = []
        for pair in ctx.pairs:
            if ctx.is_claimed(pair):
                continue
            # descriptive sentences kept only for their defined terms carry
            # default (unknown) frames — never treat them as prescriptions
            if not (pair.a.is_instruction and pair.b.is_instruction):
                continue
            d = find_disagreement(pair.a, pair.b)
            if d is None:
                continue
            f = self._route(pair, d)
            if f is not None:
                ctx.claim(pair)
                out.append(f)
        return out

    def _route(self, pair: UnitPair, d) -> Finding | None:
        a, b = pair.a, pair.b
        exposure = EXPOSURE.get(pair.co_active, 0.5)
        prec = pair.precedence

        # numeric clashes are the crispest class — route first
        if d.kind == "numeric":
            if prec.kind == PrecedenceKind.RESOLVED:
                return None
            return self._finding(
                pair,
                "DTC03",
                f"Numeric constraints disagree: {d.detail}.",
                Severity.ERROR if exposure >= 0.5 else Severity.WARNING,
                note_a=f"says {d.a_quantity.raw}" if d.a_quantity else "",
                note_b=f"says {d.b_quantity.raw}" if d.b_quantity else "",
            )

        # a declared hierarchy that resolves the pair = working as intended
        if prec.kind == PrecedenceKind.RESOLVED:
            return None

        cross_mech = prec.kind == PrecedenceKind.UNDOCUMENTED

        # soft tone tension never escalates past advisory, wherever it lives
        if d.kind == "tension":
            return self._finding(
                pair,
                "DTC08",
                f"Soft prescriptions pull in opposite directions: {d.detail}.",
                Severity.ADVISORY,
            )

        # permission-widening across precedence levels: security gate, checked
        # before the generic permit/forbid class. Raw tier numbers are only
        # meaningful within one mechanism (and positional mechanisms invert
        # them), so fire only when the pair's own precedence relation names
        # the forbidding side as the winner.
        forbid_u, permit_u = None, None
        for u, v in ((a, b), (b, a)):
            if u.frame.modality == Modality.FORBID and v.frame.modality == Modality.PERMIT:
                forbid_u, permit_u = u, v
        if (
            forbid_u is not None
            and permit_u is not None
            and prec.kind in {PrecedenceKind.RESOLVED, PrecedenceKind.POSITIONAL}
            and prec.higher is forbid_u
            and forbid_u.frame.strength.value == "hard"
        ):
            return self._finding(
                pair,
                "DTX02",
                (
                    f"A lower-precedence unit permits what a higher-precedence "
                    f"unit forbids: {d.detail}."
                ),
                Severity.ERROR,
            )

        # conditional conflict: both sides carry distinct guards
        ca = a.frame.condition.strip().lower()
        cb = b.frame.condition.strip().lower()
        conditional = (ca or cb) and ca != cb

        srel = scope_relation(a, b)
        # identical scopes are the same-scope case, not a partial overlap
        both_path_overlap = (
            pair.co_active == CoActiveClass.CONDITIONAL_OVERLAPPING
            and a.activation.mode == ActivationMode.PATH
            and b.activation.mode == ActivationMode.PATH
            and srel != "equal"
        )

        if cross_mech:
            if a.file.mechanism != b.file.mechanism:
                surfaces = f"{a.file.mechanism} vs {b.file.mechanism}"
            else:
                surfaces = f"{a.file.path} vs {b.file.path}"
            return self._finding(
                pair,
                "DTP04",
                f"Cross-layer collision ({surfaces}): {d.detail}.",
                Severity.WARNING,
                witness=_witness(pair) if conditional else "",
            )

        if srel in {"equal", "unknown"} and not conditional and not both_path_overlap:
            if d.permit_involved:
                return self._finding(
                    pair,
                    "DTC05",
                    f"Permit vs forbid on the same action: {d.detail}.",
                    Severity.WARNING,
                )
            if not d.hard:
                return self._finding(
                    pair,
                    "DTC08",
                    f"Soft prescriptions pull in opposite directions: {d.detail}.",
                    Severity.ADVISORY,
                )
            return self._finding(
                pair,
                "DTC01",
                f"Direct contradiction: {d.detail}.",
                Severity.ERROR if exposure >= 0.7 else Severity.WARNING,
            )

        if conditional and srel in {"equal", "unknown"}:
            # a deliberate carve-out ("only when reverting a broken deploy",
            # "unless the user asks") on exactly one side is an intentional
            # exception to the other's default — fragile, not conflicting
            exc_a = has_exception_marker(a.text)
            exc_b = has_exception_marker(b.text)
            if exc_a != exc_b:
                return self._finding(
                    pair,
                    "DTP03",
                    (
                        f"Fragile exception: one side carves a deliberate exception out "
                        f"of the other's default ({d.detail}); nothing but wording "
                        f"protects the carve-out."
                    ),
                    Severity.ADVISORY,
                    witness=_witness(pair),
                )
            return self._finding(
                pair,
                "DTC02",
                f"Conditional conflict: {d.detail} when both guards hold.",
                Severity.WARNING,
                witness=_witness(pair),
            )

        # scope-structured cases (the Al-Shaer transplant)
        if srel in {"a-subset-of-b", "b-subset-of-a"}:
            narrow, broad = (a, b) if srel == "a-subset-of-b" else (b, a)
            winner = prec.higher
            if winner is not None and winner is broad:
                return self._finding(
                    pair,
                    "DTP01",
                    (
                        f"Shadowed instruction: the broader, higher-precedence unit fully "
                        f"covers the narrower one with a different prescription "
                        f"({d.detail}) — the narrow one can never take effect."
                    ),
                    Severity.WARNING,
                )
            if (winner is not None and winner is narrow) or has_exception_marker(narrow.text):
                return self._finding(
                    pair,
                    "DTP03",
                    (
                        f"Fragile exception: a narrow unit carves an exception out of a "
                        f"broad opposite default ({d.detail}) with only positional "
                        f"precedence protecting it."
                    ),
                    Severity.ADVISORY,
                )
            return self._finding(
                pair,
                "DTP02",
                (
                    f"Precedence ambiguity on nested scopes: {d.detail}; no declared "
                    f"resolution order."
                ),
                Severity.WARNING,
                witness=_witness(pair),
            )

        # partial overlap (correlation) or distinct conditions
        return self._finding(
            pair,
            "DTP02",
            (
                f"Precedence ambiguity: scopes partially overlap and prescriptions "
                f"disagree ({d.detail}); the outcome in the intersection depends on an "
                f"undeclared resolution order."
            ),
            Severity.WARNING,
            witness=_witness(pair),
        )

    def _finding(
        self,
        pair: UnitPair,
        code: str,
        message: str,
        severity: Severity,
        witness: str = "",
        note_a: str = "",
        note_b: str = "",
    ) -> Finding:
        return Finding(
            code=code,
            message=message,
            severity=severity,
            evidence=pair_evidence(pair, note_a, note_b),
            units=[pair.a, pair.b],
            co_activation=pair.co_activation_account,
            precedence=pair.precedence.account,
            suggestion=_suggest(pair, code),
            witness=witness,
            lanes=("deterministic",),
        )


class FormatConflictDetector(Detector):
    """DTC04: mutually unsatisfiable exclusive output-format constraints."""

    codes = ("DTC04",)
    name = "format-conflict"

    def run(self, ctx: AnalysisContext) -> list[Finding]:
        out: list[Finding] = []
        for pair in ctx.pairs:
            if ctx.is_claimed(pair):
                continue
            if not (pair.a.is_instruction and pair.b.is_instruction):
                continue
            fa = _format_constraint(pair.a)
            fb = _format_constraint(pair.b)
            if not fa or not fb:
                continue
            (tok_a, fam_a), (tok_b, fam_b) = fa, fb
            if tok_a == tok_b:
                continue
            if pair.precedence.kind == PrecedenceKind.RESOLVED:
                continue
            ctx.claim(pair)
            out.append(
                Finding(
                    code="DTC04",
                    message=(
                        f"Exclusive output-format constraints collide: one requires "
                        f"'{tok_a}'-only output, the other '{tok_b}'"
                        + ("" if fam_a == fam_b else f" ({fam_a} vs {fam_b}")
                        + (")" if fam_a != fam_b else "")
                        + "."
                    ),
                    severity=Severity.ERROR,
                    evidence=pair_evidence(pair, f"requires {tok_a}", f"requires {tok_b}"),
                    units=[pair.a, pair.b],
                    co_activation=pair.co_activation_account,
                    precedence=pair.precedence.account,
                    suggestion=(
                        "Scope each format requirement to its context (e.g. 'API "
                        "responses: JSON; explanations: prose') or drop one."
                    ),
                )
            )
        return out


_FORMAT_TOKEN_RES = {
    tok: re.compile(r"\b" + re.escape(tok).replace(r"\ ", r"\s+") + r"\b", re.IGNORECASE)
    for tok in FORMAT_TOKENS
}


def _format_constraint(u: InstructionUnit) -> tuple[str, str] | None:
    """(token, family) if the unit imposes an exclusive output-format constraint."""
    if u.frame.modality not in {Modality.OBLIGE, Modality.FORBID}:
        return None
    if u.frame.modality == Modality.FORBID:
        return None  # "never use emojis" restricts but doesn't demand a format
    text = u.text
    if not FORMAT_EXCLUSIVE_RE.search(text):
        return None
    if not re.search(
        r"\b(output|respond|response|reply|replies|answer|format|write|return)\w*\b",
        text,
        re.IGNORECASE,
    ):
        return None
    hits = [(tok, fam) for tok, fam in FORMAT_TOKENS.items() if _FORMAT_TOKEN_RES[tok].search(text)]
    if len(hits) != 1:
        return None  # multiple formats mentioned = probably enumerating options
    return hits[0]
