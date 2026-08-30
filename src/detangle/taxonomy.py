"""The detangle conflict taxonomy: error codes, default severities, docs.

Codes are ``DT<class><nn>`` where class is one of:

- **C** — Conflicts: pairwise or k-wise prescriptive incompatibility
- **P** — Precedence & reachability (order-aware)
- **R** — Redundancy & drift
- **S** — Selection & routing (model-triggered activation)
- **X** — Security-adjacent

Synthesized from firewall-conflict algebra (Al-Shaer & Hamed), policy
modality conflicts (Lupu & Sloman), NLP contradiction categories
(de Marneffe), PolicyLint subsumption relations, and agent-config field
studies. See docs/taxonomy.md for the full write-up.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


class Severity(IntEnum):
    """Ordered so max() picks the worst."""

    INFO = 0
    ADVISORY = 1
    WARNING = 2
    ERROR = 3

    @property
    def label(self) -> str:
        return self.name.lower()


@dataclass(frozen=True)
class Rule:
    code: str
    name: str
    summary: str
    default_severity: Severity
    pairwise: bool  # findings reference >=2 units (vs single-unit smells)


_RULES: list[Rule] = [
    # ---- Class C: conflicts -------------------------------------------------
    Rule(
        "DTC01",
        "direct-contradiction",
        "Two co-active instructions prescribe incompatible behavior for the same "
        "scope ('always X' vs 'never X').",
        Severity.ERROR,
        True,
    ),
    Rule(
        "DTC02",
        "conditional-conflict",
        "Individually satisfiable instructions become jointly unsatisfiable when a "
        "boundary condition holds; the witness scenario is the finding.",
        Severity.WARNING,
        True,
    ),
    Rule(
        "DTC03",
        "quantitative-conflict",
        "Numeric or limit disagreement between co-active instructions "
        "('at most 3 retries' vs 'exactly 5 retries').",
        Severity.ERROR,
        True,
    ),
    Rule(
        "DTC04",
        "format-conflict",
        "Mutually unsatisfiable output-format constraints "
        "('respond with JSON only' vs 'explain your reasoning in prose').",
        Severity.ERROR,
        True,
    ),
    Rule(
        "DTC05",
        "modality-conflict",
        "Permit vs forbid vs oblige on the same (action, object) with overlapping scope.",
        Severity.WARNING,
        True,
    ),
    Rule(
        "DTC06",
        "impossible-instruction",
        "An obligation that cannot be satisfied given stated facts or available tools.",
        Severity.WARNING,
        False,
    ),
    Rule(
        "DTC07",
        "higher-order-set",
        "Three or more instructions that are pairwise consistent but jointly unsatisfiable.",
        Severity.WARNING,
        True,
    ),
    Rule(
        "DTC08",
        "pragmatic-tension",
        "Soft conflict: jointly satisfiable but mutually degrading "
        "('be concise' vs 'always explain in detail'). Advisory only.",
        Severity.ADVISORY,
        True,
    ),
    # ---- Class P: precedence & reachability ---------------------------------
    Rule(
        "DTP01",
        "shadowed-instruction",
        "A higher-precedence unit fully covers a lower-precedence one with a "
        "different prescription; the lower one can never take effect.",
        Severity.WARNING,
        True,
    ),
    Rule(
        "DTP02",
        "precedence-ambiguity",
        "Partial scope overlap with different prescriptions and no declared "
        "resolution order; the outcome is model- and position-dependent.",
        Severity.WARNING,
        True,
    ),
    Rule(
        "DTP03",
        "fragile-exception",
        "A narrow exception coexists with a broad opposite default with no "
        "declared precedence; works today, breaks under reordering.",
        Severity.ADVISORY,
        True,
    ),
    Rule(
        "DTP04",
        "cross-layer-conflict",
        "Units in different layers (memory vs skill vs subagent vs tool "
        "description) collide; the verdict depends on often-undocumented "
        "cross-mechanism precedence.",
        Severity.WARNING,
        True,
    ),
    Rule(
        "DTP05",
        "divergent-interpretation",
        "The same repository yields different active instruction sets under "
        "different tools (e.g. Zed reads one file, Codex concatenates).",
        Severity.ADVISORY,
        False,
    ),
    Rule(
        "DTP06",
        "unreachable-instruction",
        "Never (or unreliably) loaded due to discovery, size, or truncation "
        "budgets — the instruction cannot reach the model.",
        Severity.WARNING,
        False,
    ),
    # ---- Class R: redundancy & drift ----------------------------------------
    Rule(
        "DTR01",
        "duplicate",
        "Same condition and same prescription stated twice; harmless today, "
        "divergence risk on edit, measured token cost for no gain.",
        Severity.ADVISORY,
        True,
    ),
    Rule(
        "DTR02",
        "near-duplicate-drift",
        "Paraphrase pair that has started to diverge after edits — a merge "
        "conflict in slow motion.",
        Severity.WARNING,
        True,
    ),
    Rule(
        "DTR03",
        "terminology-inconsistency",
        "The same term is defined or used differently across files, or two names "
        "are used for one concept.",
        Severity.ADVISORY,
        True,
    ),
    Rule(
        "DTR04",
        "lint-leakage",
        "Restates what a deterministic enforcer (linter, hook, formatter) already guarantees.",
        Severity.INFO,
        False,
    ),
    Rule(
        "DTR05",
        "stale-reference",
        "Points at files, commands, or tools that do not exist in the repository.",
        Severity.WARNING,
        False,
    ),
    # ---- Class S: selection & routing ---------------------------------------
    Rule(
        "DTS01",
        "trigger-overlap",
        "Skill/rule trigger descriptions claim the same intents or keywords, "
        "making routing nondeterministic.",
        Severity.WARNING,
        True,
    ),
    Rule(
        "DTS02",
        "description-mismatch",
        "A skill/subagent description promises something its body does not "
        "deliver — routing on false pretenses.",
        Severity.ADVISORY,
        False,
    ),
    Rule(
        "DTS03",
        "shadowed-name",
        "Cross-level name shadowing (e.g. a personal skill silently overriding a "
        "project skill of the same name).",
        Severity.WARNING,
        True,
    ),
    # ---- Class X: security-adjacent -----------------------------------------
    Rule(
        "DTX01",
        "hidden-instruction",
        "Invisible Unicode, HTML-comment payloads, or injected authority claims "
        "inside a config file.",
        Severity.ERROR,
        False,
    ),
    Rule(
        "DTX02",
        "permission-widening",
        "A lower-tier unit grants more than a higher tier allows.",
        Severity.ERROR,
        True,
    ),
]

RULES: dict[str, Rule] = {r.code: r for r in _RULES}


def rule(code: str) -> Rule:
    return RULES[code]


def all_codes() -> list[str]:
    return [r.code for r in _RULES]
