"""The co-activation engine and precedence model.

A conflict matters only when both units can be simultaneously in the model's
context. This module answers, for any unit pair:

1. **co-activation**: can they co-load, and under what conditions? (exact
   for glob scopes and context isolation; "potentially co-active" for
   model-triggered descriptions)
2. **precedence**: if they do co-load and disagree, does the ecosystem
   declare a winner? Four answers: resolved / positional / ambiguous /
   undocumented — and findings phrase themselves accordingly.

Per-mechanism conflict-resolution semantics (verified against vendor docs):

===========================  =====================================================
mechanism                    documented conflict rule
===========================  =====================================================
claude-code memory           concatenation; "if two rules contradict each other,
                             Claude may pick one arbitrarily" -> AMBIGUOUS
claude-code rules            user rules load before project rules (positional);
                             same-level order unspecified -> AMBIGUOUS
claude-code skills           name-shadowing across levels is deterministic, but
                             co-triggering different skills has NO documented
                             arbitration -> AMBIGUOUS
claude-code subagents        isolated contexts -> pairs never co-active
agents-md (Codex reading)    "closer files override earlier guidance because they
                             appear later in the combined prompt" -> POSITIONAL
cursor rules                 merge-all; Team > Project > User soft priority;
                             same-level ordering UNSPECIFIED -> AMBIGUOUS
copilot instructions         "all sets of relevant instructions are provided" ->
                             AMBIGUOUS (union, soft priority only)
cross-mechanism              UNDOCUMENTED everywhere
===========================  =====================================================
"""

from __future__ import annotations

from .globs import glob_sets_intersect
from .ir import (
    ActivationMode,
    CoActiveClass,
    Ecosystem,
    InstructionUnit,
    PrecedenceKind,
    PrecedenceRelation,
    UnitPair,
)
from .lexicons import content_tokens

# exposure weights per co-activation class (feeds severity fusion)
EXPOSURE: dict[CoActiveClass, float] = {
    CoActiveClass.ALWAYS_ALWAYS: 1.0,
    CoActiveClass.ALWAYS_CONDITIONAL: 0.7,
    CoActiveClass.CONDITIONAL_OVERLAPPING: 0.5,
    CoActiveClass.CROSS_TOOL_ONLY: 0.3,
    CoActiveClass.MUTUALLY_EXCLUSIVE: 0.0,
}


def _readers(u: InstructionUnit) -> frozenset[str]:
    return frozenset(u.file.meta.get("readers", ()))


def _context_scope(u: InstructionUnit) -> str:
    return str(u.file.meta.get("context_scope", ""))


def description_overlap(a: str, b: str) -> float:
    """Jaccard overlap of content tokens between two trigger descriptions."""
    ta, tb = set(content_tokens(a)), set(content_tokens(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def co_activation(a: InstructionUnit, b: InstructionUnit) -> tuple[CoActiveClass, str]:
    """Classify a pair's co-activation and produce the human-readable account."""
    common = _readers(a) & _readers(b)
    if not common:
        return (
            CoActiveClass.CROSS_TOOL_ONLY,
            f"no tool reads both files ({a.file.path} ← {', '.join(sorted(_readers(a))) or '?'}; "
            f"{b.file.path} ← {', '.join(sorted(_readers(b))) or '?'})",
        )

    sa, sb = _context_scope(a), _context_scope(b)
    if sa and sb and sa != sb:
        return (
            CoActiveClass.MUTUALLY_EXCLUSIVE,
            f"{sa} and {sb} run in separate contexts and never co-load",
        )
    if (sa and not sb and b.file.mechanism not in {"memory"}) or (
        sb and not sa and a.file.mechanism not in {"memory"}
    ):
        # a subagent context receives the CLAUDE.md hierarchy but not
        # main-context skills/rules/commands
        scoped = sa or sb
        other = b if sa else a
        return (
            CoActiveClass.MUTUALLY_EXCLUSIVE,
            f"{scoped} runs in an isolated context that does not receive "
            f"{other.file.mechanism} content",
        )

    ma, mb = a.activation.mode, b.activation.mode
    tools = "/".join(sorted(common))

    if ma == ActivationMode.ALWAYS and mb == ActivationMode.ALWAYS:
        return (
            CoActiveClass.ALWAYS_ALWAYS,
            f"both load at launch under {tools}",
        )

    if ActivationMode.ALWAYS in (ma, mb):
        cond = b if ma == ActivationMode.ALWAYS else a
        detail = _condition_phrase(cond)
        return (
            CoActiveClass.ALWAYS_CONDITIONAL,
            f"one loads at launch; the other {detail} (under {tools})",
        )

    if ma == ActivationMode.PATH and mb == ActivationMode.PATH:
        if glob_sets_intersect(a.activation.globs, b.activation.globs):
            return (
                CoActiveClass.CONDITIONAL_OVERLAPPING,
                f"path scopes intersect: {_globs(a)} ∩ {_globs(b)} is non-empty",
            )
        return (
            CoActiveClass.MUTUALLY_EXCLUSIVE,
            f"path scopes are disjoint: {_globs(a)} vs {_globs(b)}",
        )

    if ActivationMode.MODEL in (ma, mb) and ActivationMode.PATH in (ma, mb):
        return (
            CoActiveClass.CONDITIONAL_OVERLAPPING,
            "a path-scoped unit and a description-triggered unit can plausibly co-fire",
        )

    if ma == ActivationMode.MODEL and mb == ActivationMode.MODEL:
        sim = description_overlap(a.activation.description, b.activation.description)
        return (
            CoActiveClass.CONDITIONAL_OVERLAPPING,
            f"both are description-triggered (trigger overlap {sim:.0%}); "
            "potentially co-active — no ecosystem documents arbitration",
        )

    # USER mode combinations: user can invoke alongside anything
    return (
        CoActiveClass.CONDITIONAL_OVERLAPPING,
        "a user-invoked unit can co-occur with the other's activation",
    )


def _condition_phrase(u: InstructionUnit) -> str:
    if u.activation.mode == ActivationMode.PATH:
        return f"co-fires when working under {_globs(u)}"
    if u.activation.mode == ActivationMode.MODEL:
        return "is description-triggered"
    return "is user-invoked"


def _globs(u: InstructionUnit) -> str:
    return ", ".join(u.activation.globs) or "(no globs)"


# ---------------------------------------------------------------------------
# Precedence
# ---------------------------------------------------------------------------

_ARBITRARY_QUOTE = (
    'Anthropic: "if two rules contradict each other, Claude may pick one arbitrarily"'
)


def precedence(a: InstructionUnit, b: InstructionUnit) -> PrecedenceRelation:
    """What (if anything) resolves a disagreement between these two units?"""
    fa, fb = a.file, b.file

    if fa.path == fb.path:
        return PrecedenceRelation(
            PrecedenceKind.AMBIGUOUS,
            account="both instructions live in the same file; no declared precedence between them",
        )

    if fa.ecosystem != fb.ecosystem:
        return PrecedenceRelation(
            PrecedenceKind.UNDOCUMENTED,
            account=(
                f"{fa.path} and {fb.path} belong to different config surfaces; any tool "
                "reading both provides them side by side with no documented precedence"
            ),
        )

    if fa.mechanism != fb.mechanism:
        return PrecedenceRelation(
            PrecedenceKind.UNDOCUMENTED,
            account=(
                f"cross-mechanism pair ({fa.mechanism} vs {fb.mechanism}): no ecosystem "
                "documents which one wins"
            ),
        )

    mech = fa.mechanism
    eco = fa.ecosystem

    if eco == Ecosystem.CLAUDE_CODE and mech == "memory":
        return PrecedenceRelation(
            PrecedenceKind.AMBIGUOUS,
            account=(
                "CLAUDE.md contents concatenate (root → working directory); "
                f"contradictions are not resolved — {_ARBITRARY_QUOTE}"
            ),
        )

    if eco == Ecosystem.CLAUDE_CODE and mech == "rules":
        if fa.tier != fb.tier:
            # rules are POSITIONAL: project rules load after user rules, so
            # the HIGHER memory-tier number (project=20 > user=10) wins here —
            # unlike name-shadowing mechanisms where lower tier wins
            hi = a if fa.tier > fb.tier else b
            return PrecedenceRelation(
                PrecedenceKind.POSITIONAL,
                higher=hi,
                account="project rules load after user rules (positional priority only)",
            )
        return PrecedenceRelation(
            PrecedenceKind.AMBIGUOUS,
            account="both are project rules; ordering among same-level rules is unspecified",
        )

    if eco == Ecosystem.CLAUDE_CODE and mech == "skill":
        if fa.tier != fb.tier:
            hi = a if fa.tier < fb.tier else b
            return PrecedenceRelation(
                PrecedenceKind.RESOLVED,
                higher=hi,
                account="skill name-shadowing: enterprise overrides personal overrides project",
            )
        return PrecedenceRelation(
            PrecedenceKind.AMBIGUOUS,
            account=(
                "different skills at the same level; no ecosystem documents arbitration "
                "between co-triggering skills"
            ),
        )

    if eco == Ecosystem.AGENTS_MD:
        if fa.load_order != fb.load_order:
            hi = a if fa.load_order > fb.load_order else b
            return PrecedenceRelation(
                PrecedenceKind.POSITIONAL,
                higher=hi,
                account=(
                    "Codex-style reading: files closer to the working directory override "
                    "earlier guidance because they appear later in the combined prompt — "
                    "but Copilot applies nearest-wins and Zed reads one file only"
                ),
            )
        return PrecedenceRelation(
            PrecedenceKind.AMBIGUOUS,
            account="same AGENTS.md level; the standard does not define merge semantics",
        )

    if eco == Ecosystem.CURSOR:
        return PrecedenceRelation(
            PrecedenceKind.AMBIGUOUS,
            account=(
                "Cursor merges all applicable rules; Team > Project > User is soft "
                "priority and same-level ordering is unspecified"
            ),
        )

    if eco == Ecosystem.COPILOT:
        return PrecedenceRelation(
            PrecedenceKind.AMBIGUOUS,
            account=(
                'Copilot: "all sets of relevant instructions are provided" — everything '
                "co-loads; priority is advisory only"
            ),
        )

    return PrecedenceRelation(
        PrecedenceKind.UNDOCUMENTED,
        account=f"no documented conflict-resolution rule for {eco.value}/{mech}",
    )


def build_pair(a: InstructionUnit, b: InstructionUnit, similarity: float = 0.0) -> UnitPair:
    co, account = co_activation(a, b)
    return UnitPair(
        a=a,
        b=b,
        co_active=co,
        co_activation_account=account,
        precedence=precedence(a, b),
        similarity=similarity,
    )


def scope_relation(a: InstructionUnit, b: InstructionUnit) -> str:
    """Al-Shaer-style scope relation between two units' activation scopes.

    Returns one of: 'equal', 'a-subset-of-b', 'b-subset-of-a', 'overlap',
    'disjoint', 'unknown'. Only PATH/ALWAYS scopes are comparable exactly;
    MODEL scopes are 'unknown' (probabilistic).
    """
    from .globs import glob_set_subset

    ma, mb = a.activation.mode, b.activation.mode
    if ma == ActivationMode.MODEL or mb == ActivationMode.MODEL:
        return "unknown"
    ga = a.activation.globs if ma == ActivationMode.PATH else ("**",)
    gb = b.activation.globs if mb == ActivationMode.PATH else ("**",)
    if set(ga) == set(gb):
        return "equal"
    a_in_b = glob_set_subset(ga, gb)
    b_in_a = glob_set_subset(gb, ga)
    if a_in_b and b_in_a:
        return "equal"
    if a_in_b:
        return "a-subset-of-b"
    if b_in_a:
        return "b-subset-of-a"
    if glob_sets_intersect(ga, gb):
        return "overlap"
    return "disjoint"
