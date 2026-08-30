"""The Instruction Unit intermediate representation.

Everything downstream of ingestion operates on these records. Design notes:

- Units are content-addressed (``unit.uid``) so verdict caches and diff mode
  can key off (text, source) identity.
- ``Activation`` captures *when* a unit is in the model's context; the
  co-activation engine works exclusively off this record, never raw files.
- ``Frame`` is the deontic tuple (modality, strength, action, object, ...)
  used by the deterministic conflict detectors. Extraction is best-effort;
  detectors must treat missing fields as "unknown", never as "matches".
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Ecosystem(str, Enum):
    """Which tool's configuration surface a file belongs to."""

    CLAUDE_CODE = "claude-code"
    AGENTS_MD = "agents-md"
    CURSOR = "cursor"
    COPILOT = "copilot"
    GENERIC = "generic"


class Layer(str, Enum):
    """Configuration layer within an ecosystem (see docs/ecosystems.md)."""

    MANAGED = "managed"
    USER_GLOBAL = "user_global"
    PROJECT = "project"
    LOCAL = "local"
    SUBDIR = "subdir"
    RULES = "rules"
    SKILL = "skill"
    SUBAGENT = "subagent"
    PLUGIN = "plugin"
    TOOL_DESC = "tool_desc"
    MCP_INSTRUCTIONS = "mcp_instructions"
    OUTPUT_STYLE = "output_style"


class ActivationMode(str, Enum):
    """How a unit gets into context (appendix/07 four activation classes)."""

    ALWAYS = "always"  # loaded at launch, unconditionally
    PATH = "path"  # glob-triggered (rules paths:, Cursor auto-attach, Copilot applyTo)
    MODEL = "model"  # description-triggered (skills, subagents, Cursor agent-requested)
    USER = "user"  # manual (@-mention, /command, explicit invocation)


class BudgetRisk(str, Enum):
    """Whether truncation/discovery budgets can silently drop this unit."""

    NONE = "none"
    TRUNCATION = "truncation"  # may be cut by a size cap (32 KiB halt, 6k/12k chars...)
    COMPACTION = "compaction"  # may be dropped on context compaction
    LISTING = "listing"  # its *trigger* (description) may be truncated in a listing


class Modality(str, Enum):
    """Deontic modality of the instruction (PolicyLint / Lupu-Sloman transplant)."""

    OBLIGE = "oblige"  # must / always / ensure
    FORBID = "forbid"  # never / must not / don't
    PERMIT = "permit"  # may / can / it's ok to
    PREFER = "prefer"  # should / avoid / prefer — soft; never hard-conflicts


class Strength(str, Enum):
    HARD = "hard"  # MUST / NEVER / ALWAYS
    SOFT = "soft"  # should / avoid / prefer / try to


@dataclass(frozen=True)
class SourceSpan:
    """A file location, 1-based inclusive lines."""

    path: str  # repo-relative, posix separators
    start_line: int
    end_line: int

    def __str__(self) -> str:  # pragma: no cover - display helper
        if self.start_line == self.end_line:
            return f"{self.path}:{self.start_line}"
        return f"{self.path}:{self.start_line}-{self.end_line}"


@dataclass
class Activation:
    """When this unit is in context."""

    mode: ActivationMode = ActivationMode.ALWAYS
    globs: tuple[str, ...] = ()  # for PATH mode
    description: str = ""  # for MODEL mode: the trigger description text
    budget_risk: BudgetRisk = BudgetRisk.NONE
    budget_note: str = ""  # human-readable account of the risk


@dataclass
class Quantity:
    """An extracted numeric constraint: ``comparator value unit`` about a subject."""

    value: float
    unit: str = ""  # normalized unit token ("retries", "seconds", "lines", "")
    comparator: str = "=="  # one of ==, <=, >=, <, >, ~ (approx)
    subject: str = ""  # what the number constrains, best-effort ("retries", "timeout")
    raw: str = ""  # original matched text


@dataclass
class Frame:
    """Deontic tuple extracted from a unit (best-effort; fields may be empty)."""

    modality: Modality = Modality.OBLIGE
    strength: Strength = Strength.HARD
    negated: bool = False  # surface negation on the action
    action: str = ""  # lemma-ish verb ("push", "use", "write")
    obj: str = ""  # normalized object/resource ("main_branch", "tests")
    recipient: str = ""
    condition: str = ""  # guard text ("when releasing", "in src/api")
    raw_verb: str = ""  # surface verb before normalization


@dataclass
class ConfigFile:
    """One parsed configuration artifact."""

    path: str  # repo-relative posix path
    ecosystem: Ecosystem
    layer: Layer
    tier: int  # per-ecosystem precedence tier; LOWER = higher precedence
    activation: Activation
    text: str  # raw file text (post import-resolution markers removed)
    meta: dict[str, Any] = field(default_factory=dict)  # frontmatter etc.
    mechanism: str = "memory"  # precedence mechanism family, e.g. "memory", "skill", "subagent"
    tool: str = ""  # concrete tool if ecosystem-specific ("claude-code", "codex", ...)
    load_order: int = 0  # position in concatenation order (0 = injected first)
    notes: list[str] = field(default_factory=list)  # parser diagnostics


@dataclass
class InstructionUnit:
    """One atomic instruction."""

    text: str  # verbatim instruction text
    normalized: str  # declarative normalization ("The agent must not ...")
    span: SourceSpan
    file: ConfigFile
    activation: Activation
    frame: Frame = field(default_factory=Frame)
    quantities: list[Quantity] = field(default_factory=list)
    topics: tuple[str, ...] = ()
    heading: str = ""  # nearest markdown heading path ("Git > Commits")
    is_instruction: bool = True  # False = contextual/descriptive sentence kept for terms
    defined_terms: tuple[str, ...] = ()  # terms this unit defines (DTR03)

    _uid: str = field(default="", repr=False)

    @property
    def uid(self) -> str:
        """Content-addressed id: sha256(normalized text + source path), 12 hex chars."""
        if not self._uid:
            h = hashlib.sha256(
                (self.normalized.strip().lower() + "\x00" + self.file.path).encode("utf-8")
            ).hexdigest()[:12]
            object.__setattr__(self, "_uid", h)
        return self._uid

    @property
    def tier(self) -> int:
        return self.file.tier

    @property
    def layer(self) -> Layer:
        return self.file.layer

    @property
    def ecosystem(self) -> Ecosystem:
        return self.file.ecosystem

    def short(self, limit: int = 80) -> str:
        t = " ".join(self.text.split())
        return t if len(t) <= limit else t[: limit - 1] + "…"


class CoActiveClass(str, Enum):
    """Exposure classes for a unit pair (PLAN.md §4)."""

    ALWAYS_ALWAYS = "always-always"  # both in the launch set: highest exposure
    ALWAYS_CONDITIONAL = "always-conditional"
    CONDITIONAL_OVERLAPPING = "conditional-overlapping"  # globs intersect / descriptions co-fire
    MUTUALLY_EXCLUSIVE = "mutually-exclusive"  # provably never co-active: prune
    CROSS_TOOL_ONLY = "cross-tool-only"  # co-active only if different tools read the repo


class PrecedenceKind(str, Enum):
    RESOLVED = "resolved"  # declared hierarchy resolves the pair
    POSITIONAL = "positional"  # only resolved by concatenation order ("later wins", soft)
    AMBIGUOUS = "ambiguous"  # co-equal, no declared order
    UNDOCUMENTED = "undocumented"  # the ecosystem does not specify resolution


@dataclass
class PrecedenceRelation:
    kind: PrecedenceKind
    higher: InstructionUnit | None = None  # unit that wins, if resolved/positional
    account: str = ""  # human-readable explanation


@dataclass
class UnitPair:
    """A candidate pair with its co-activation and precedence account."""

    a: InstructionUnit
    b: InstructionUnit
    co_active: CoActiveClass
    co_activation_account: str = ""
    precedence: PrecedenceRelation = field(
        default_factory=lambda: PrecedenceRelation(PrecedenceKind.AMBIGUOUS)
    )
    similarity: float = 0.0  # lexical similarity used during blocking
    block_keys: tuple[str, ...] = ()  # which blockers produced this pair

    @property
    def key(self) -> str:
        """Order-independent cache key for the pair."""
        u, v = sorted((self.a.uid, self.b.uid))
        return f"{u}:{v}"
