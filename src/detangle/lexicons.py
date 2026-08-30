"""Wordlists and lexical tables for deterministic instruction analysis.

Sources: NASA ARM imperative-strength ranking (shall > must > will > should),
QuARS/Femmer requirements-smell indicator lists, de Marneffe's Category-1
contradiction cues (antonymy, negation, numeric mismatch), and the
agent-config domain itself. These lists gate *deterministic* detectors, so
they are curated for precision: a miss costs recall (the NLI/jury lanes can
recover it); a bad entry costs a false positive.
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Modality & strength markers
# ---------------------------------------------------------------------------
# Order matters: first match wins. Patterns are matched case-insensitively
# against the start of the (whitespace-normalized) instruction, or anywhere
# for embedded modal verbs.

FORBID_HARD = (
    r"\bnever\b",
    r"\bmust\s+not\b",
    r"\bmust\s+never\b",
    r"\bdo\s+not\b",
    r"\bdon['’]t\b",
    r"\bshall\s+not\b",
    r"\bmay\s+not\b",
    r"\bnot\s+allowed\b",
    r"\bforbidden\b",
    r"\bprohibited\b",
    r"\bno\s+\w+ing\b",  # "no pushing to main"
    r"\bunder\s+no\s+circumstances\b",
)

FORBID_SOFT = (
    r"(?<!to\s)\bavoid\b",  # "to avoid X" is a purpose clause, not a prohibition
    r"\brefrain\s+from\b",
    r"\bshould\s+not\b",
    r"\bshouldn['’]t\b",
    r"\btry\s+not\s+to\b",
    r"\bsteer\s+clear\b",
    r"\bdiscouraged\b",
)

OBLIGE_HARD = (
    r"\balways\b",
    r"\bmust\b",
    r"\bshall\b",
    r"\bensure\b",
    r"\bmake\s+sure\b",
    r"\bbe\s+sure\s+to\b",
    r"\brequired\s+to\b",
    r"\bis\s+required\b",
    r"\bmandatory\b",
    r"\bhave\s+to\b",
    r"\bneeds?\s+to\b",
    r"\bit\s+is\s+critical\b",
    r"\bimportant:\s",
)

OBLIGE_SOFT = (
    r"\bshould\b",
    r"\btry\s+to\b",
    r"\baim\s+to\b",
    r"\bwhenever\s+possible\b",
    r"\bideally\b",
    r"\bstrive\s+to\b",
)

PERMIT = (
    r"\bmay\b",
    r"\bcan\b",
    r"\ballowed\s+to\b",
    r"\bit['’]?s\s+(?:ok|okay|fine)\b",
    r"\bfeel\s+free\s+to\b",
    r"\bare\s+free\s+to\b",
    r"\bpermitted\b",
    r"\boptionally\b",
)

PREFER = (
    r"\bprefer\b",
    r"\bfavou?r\b",
    r"\blean\s+towards?\b",
    r"\bdefault\s+to\b",
    r"\bwhen\s+in\s+doubt\b",
)


# ---------------------------------------------------------------------------
# Imperative cue verbs: a sentence starting with one of these (or with a
# modality marker) is treated as an instruction.
# ---------------------------------------------------------------------------

IMPERATIVE_VERBS = frozenset(
    [
        "use",
        "run",
        "write",
        "add",
        "remove",
        "delete",
        "create",
        "keep",
        "make",
        "put",
        "set",
        "avoid",
        "prefer",
        "ensure",
        "check",
        "test",
        "verify",
        "commit",
        "push",
        "pull",
        "merge",
        "rebase",
        "branch",
        "tag",
        "deploy",
        "install",
        "update",
        "upgrade",
        "pin",
        "document",
        "explain",
        "describe",
        "include",
        "exclude",
        "omit",
        "skip",
        "follow",
        "respect",
        "ask",
        "confirm",
        "prompt",
        "notify",
        "warn",
        "log",
        "print",
        "return",
        "respond",
        "reply",
        "answer",
        "format",
        "indent",
        "name",
        "call",
        "invoke",
        "execute",
        "open",
        "close",
        "read",
        "edit",
        "review",
        "lint",
        "build",
        "compile",
        "release",
        "publish",
        "version",
        "bump",
        "note",
        "remember",
        "consider",
        "treat",
        "handle",
        "validate",
        "sanitize",
        "escape",
        "quote",
        "wrap",
        "split",
        "join",
        "sort",
        "order",
        "group",
        "place",
        "store",
        "save",
        "load",
        "fetch",
        "download",
        "upload",
        "send",
        "email",
        "post",
        "tag",
        "label",
        "mark",
        "flag",
        "clean",
        "refactor",
        "rename",
        "move",
        "copy",
        "paste",
        "generate",
        "emit",
        "see",
        "consult",
        "refer",
        "reference",
        "be",
        "stay",
        "remain",
        "act",
        "communicate",
        "output",
        "produce",
        "limit",
        "cap",
        "restrict",
        "scope",
        "focus",
        "stick",
        "default",
        "fall",
        "fallback",
        "start",
        "stop",
        "restart",
        "enable",
        "disable",
        "turn",
        "switch",
        "toggle",
        "apply",
        "revert",
        "reset",
        "stash",
        "squash",
        "amend",
        "sign",
        "gpg",
        "import",
        "export",
        "configure",
        "config",
        "setup",
        "init",
        "prioritize",
        "favor",
        "minimize",
        "maximize",
        "batch",
        "stream",
        "cache",
        "memoize",
        "retry",
        "wait",
        "sleep",
        "poll",
        "throttle",
        "debounce",
        "prefix",
        "suffix",
        "capitalize",
        "lowercase",
        "uppercase",
        "pluralize",
        "accept",
        "reject",
        "approve",
        "deny",
        "grant",
        "revoke",
        "request",
        "require",
    ]
)

# Verbs that read as descriptive when sentence-initial in docs ("Contains the
# config...") — never treat these as imperative cues.
NON_IMPERATIVE_STARTERS = frozenset(
    ["contains", "includes", "provides", "describes", "lists", "shows", "displays", "represents"]
)


# ---------------------------------------------------------------------------
# Antonym pairs (symmetric). Matched against extracted action/object tokens
# and adverbs. Curated for the agent-config domain.
# ---------------------------------------------------------------------------

_ANTONYM_PAIRS: tuple[tuple[str, str], ...] = (
    ("always", "never"),
    ("before", "after"),
    ("enable", "disable"),
    ("enabled", "disabled"),
    ("allow", "forbid"),
    ("allow", "disallow"),
    ("add", "remove"),
    ("include", "exclude"),
    ("include", "omit"),
    ("start", "stop"),
    ("more", "less"),
    ("above", "below"),
    ("upper", "lower"),
    ("uppercase", "lowercase"),
    ("tabs", "spaces"),
    ("tab", "space"),
    ("singular", "plural"),
    ("verbose", "concise"),
    ("verbose", "terse"),
    ("detailed", "concise"),
    ("detailed", "brief"),
    ("long", "short"),
    ("many", "few"),
    ("ask", "assume"),
    ("sync", "async"),
    ("synchronous", "asynchronous"),
    ("first", "last"),
    ("earliest", "latest"),
    ("oldest", "newest"),
    ("accept", "reject"),
    ("approve", "deny"),
    ("grant", "revoke"),
    ("open", "close"),
    ("public", "private"),
    ("internal", "external"),
    ("show", "hide"),
    ("keep", "delete"),
    ("keep", "remove"),
    ("create", "delete"),
    ("push", "revert"),
    ("commit", "revert"),
    ("shallow", "deep"),
    ("minimize", "maximize"),
    ("single", "multiple"),
    ("with", "without"),
    ("do", "skip"),
)

ANTONYMS: dict[str, frozenset[str]] = {}
for _a, _b in _ANTONYM_PAIRS:
    ANTONYMS.setdefault(_a, set()).add(_b)  # type: ignore[arg-type]
    ANTONYMS.setdefault(_b, set()).add(_a)  # type: ignore[arg-type]
ANTONYMS = {k: frozenset(v) for k, v in ANTONYMS.items()}


def are_antonyms(a: str, b: str) -> bool:
    return b in ANTONYMS.get(a, frozenset())


# ---------------------------------------------------------------------------
# Output-format constraint families (DTC04). Two co-active hard constraints
# from *different* exclusive families conflict; same family + different
# member (e.g. json vs yaml as "the only output format") also conflicts.
# ---------------------------------------------------------------------------

FORMAT_TOKENS: dict[str, str] = {
    # token -> family
    "json": "structured",
    "yaml": "structured",
    "xml": "structured",
    "csv": "structured",
    "markdown": "markup",
    "html": "markup",
    "plain text": "prose",
    "plaintext": "prose",
    "prose": "prose",
    "paragraph": "prose",
    "paragraphs": "prose",
    "bullet points": "list",
    "bullets": "list",
    "bulleted list": "list",
    "numbered list": "list",
    "table": "table",
    "tables": "table",
    "code only": "code",
    "emoji": "emoji",
    "emojis": "emoji",
}

# phrases that make a format constraint exclusive ("only", "just", "nothing but")
FORMAT_EXCLUSIVE_RE = re.compile(
    r"\b(only|solely|exclusively|nothing\s+but|just|strictly|always\s+respond\s+(?:in|with)|"
    r"all\s+(?:output|responses?|replies)\s+(?:must|should)\s+be)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Numbers and units
# ---------------------------------------------------------------------------

WORD_NUMBERS: dict[str, float] = {
    "zero": 0,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "hundred": 100,
    "thousand": 1000,
    "once": 1,
    "twice": 2,
}

# comparator phrase -> normalized comparator
COMPARATOR_PHRASES: tuple[tuple[str, str], ...] = (
    (r"(?:must|should|shall|may|can)?\s*(?:not|never)\s+exceed", "<="),
    (r"exceeds?|exceeding", ">"),
    (r"at\s+most", "<="),
    (r"no\s+more\s+than", "<="),
    (r"not\s+more\s+than", "<="),
    (r"up\s+to", "<="),
    (r"a\s+maximum\s+of", "<="),
    (r"max(?:imum)?\.?\s+(?:of\s+)?", "<="),
    (r"under", "<"),
    (r"below", "<"),
    (r"(?:fewer|less)\s+than", "<"),
    (r"shorter\s+than", "<"),
    (r"within", "<="),
    (r"at\s+least", ">="),
    (r"no\s+(?:fewer|less)\s+than", ">="),
    (r"a\s+minimum\s+of", ">="),
    (r"min(?:imum)?\.?\s+(?:of\s+)?", ">="),
    (r"more\s+than", ">"),
    (r"over", ">"),
    (r"longer\s+than", ">"),
    (r"exceeding", ">"),
    (r"exactly", "=="),
    (r"precisely", "=="),
)

UNIT_ALIASES: dict[str, str] = {
    "second": "seconds",
    "seconds": "seconds",
    "sec": "seconds",
    "secs": "seconds",
    "s": "seconds",
    "minute": "minutes",
    "minutes": "minutes",
    "min": "minutes",
    "mins": "minutes",
    "hour": "hours",
    "hours": "hours",
    "hr": "hours",
    "hrs": "hours",
    "day": "days",
    "days": "days",
    "week": "weeks",
    "weeks": "weeks",
    "line": "lines",
    "lines": "lines",
    "word": "words",
    "words": "words",
    "character": "chars",
    "characters": "chars",
    "char": "chars",
    "chars": "chars",
    "token": "tokens",
    "tokens": "tokens",
    "retry": "retries",
    "retries": "retries",
    "time": "times",
    "times": "times",
    "attempt": "attempts",
    "attempts": "attempts",
    "result": "results",
    "results": "results",
    "item": "items",
    "items": "items",
    "file": "files",
    "files": "files",
    "test": "tests",
    "tests": "tests",
    "sentence": "sentences",
    "sentences": "sentences",
    "paragraph": "paragraphs",
    "paragraphs": "paragraphs",
    "space": "spaces",
    "spaces": "spaces",
    "level": "levels",
    "levels": "levels",
    "kb": "kb",
    "kib": "kb",
    "mb": "mb",
    "mib": "mb",
    "gb": "gb",
    "%": "percent",
    "percent": "percent",
    "px": "px",
    "commit": "commits",
    "commits": "commits",
    "branch": "branches",
    "branches": "branches",
    "argument": "args",
    "arguments": "args",
    "arg": "args",
    "args": "args",
    "parameter": "args",
    "parameters": "args",
    "dependency": "deps",
    "dependencies": "deps",
}

# units that measure the same latent dimension (for cross-unit comparison)
UNIT_DIMENSION: dict[str, tuple[str, float]] = {
    # unit -> (dimension, factor to base unit)
    "seconds": ("time", 1),
    "minutes": ("time", 60),
    "hours": ("time", 3600),
    "days": ("time", 86400),
    "weeks": ("time", 604800),
    "retries": ("attempts", 1),
    "times": ("attempts", 1),
    "attempts": ("attempts", 1),
    "chars": ("text", 1),
    "words": ("text", 6),  # rough equivalence used only for identical subjects
    "kb": ("bytes", 1024),
    "mb": ("bytes", 1024 * 1024),
    "gb": ("bytes", 1024 * 1024 * 1024),
}


# ---------------------------------------------------------------------------
# Condition / exception markers
# ---------------------------------------------------------------------------

CONDITION_LEADERS = (
    "when",
    "whenever",
    "if",
    "while",
    "unless",
    "before",
    "after",
    "during",
    "in case",
    "for",
    "on",
    "upon",
    "once",
    "where",
)

EXCEPTION_MARKERS_RE = re.compile(
    r"\b(unless|except|excluding|other\s+than|apart\s+from|save\s+for|but\s+not|"
    r"with\s+the\s+exception\s+of)\b",
    re.IGNORECASE,
)

# escape clauses / loopholes (QuARS "weakness", NASA ARM "weak phrases"):
# a unit carrying one of these is treated as soft for conflict purposes.
ESCAPE_CLAUSE_RE = re.compile(
    r"\b(as\s+appropriate|when\s+appropriate|if\s+appropriate|if\s+possible|"
    r"where\s+possible|as\s+needed|if\s+needed|when\s+necessary|if\s+necessary|"
    r"as\s+applicable|at\s+your\s+discretion|use\s+(?:your\s+)?(?:best\s+)?judgment|"
    r"generally|typically|usually|in\s+most\s+cases|to\s+the\s+extent\s+possible)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Topic lexicon: keyword -> topic tag. Used for blocking (shared-topic pairs)
# and for the co-activation account. Multi-word keys are matched first.
# ---------------------------------------------------------------------------

TOPIC_KEYWORDS: dict[str, str] = {
    # git & vcs
    "git": "git",
    "commit": "git",
    "commits": "git",
    "branch": "git",
    "branches": "git",
    "merge": "git",
    "rebase": "git",
    "push": "git",
    "pull request": "git",
    "pr": "git",
    "prs": "git",
    "main branch": "git",
    "master": "git",
    "stash": "git",
    "tag": "git",
    "cherry-pick": "git",
    "force-push": "git",
    "amend": "git",
    "squash": "git",
    "revert": "git",
    # testing
    "test": "testing",
    "tests": "testing",
    "testing": "testing",
    "unit test": "testing",
    "pytest": "testing",
    "jest": "testing",
    "coverage": "testing",
    "tdd": "testing",
    "mocks": "testing",
    "mock": "testing",
    "e2e": "testing",
    "integration test": "testing",
    # style & formatting of code
    "format": "code-style",
    "formatting": "code-style",
    "lint": "code-style",
    "linter": "code-style",
    "eslint": "code-style",
    "prettier": "code-style",
    "ruff": "code-style",
    "black": "code-style",
    "indent": "code-style",
    "indentation": "code-style",
    "tabs": "code-style",
    "spaces": "code-style",
    "semicolons": "code-style",
    "naming": "code-style",
    "camelcase": "code-style",
    "snake_case": "code-style",
    "type hints": "code-style",
    "typing": "code-style",
    "line length": "code-style",
    # docs & comments
    "comment": "docs",
    "comments": "docs",
    "docstring": "docs",
    "docstrings": "docs",
    "documentation": "docs",
    "readme": "docs",
    "changelog": "docs",
    "docs": "docs",
    "jsdoc": "docs",
    # output & tone
    "respond": "output",
    "response": "output",
    "reply": "output",
    "output": "output",
    "answer": "output",
    "concise": "output",
    "verbose": "output",
    "tone": "output",
    "explain": "output",
    "json": "output-format",
    "yaml": "output-format",
    "markdown": "output-format",
    "emoji": "output-format",
    "emojis": "output-format",
    "bullet": "output-format",
    # dependencies & build
    "dependency": "deps",
    "dependencies": "deps",
    "package": "deps",
    "npm": "deps",
    "yarn": "deps",
    "pnpm": "deps",
    "pip": "deps",
    "uv": "deps",
    "poetry": "deps",
    "install": "deps",
    "lockfile": "deps",
    "version": "deps",
    "build": "build",
    "compile": "build",
    "bundle": "build",
    "webpack": "build",
    # ci/cd & deploy
    "ci": "ci",
    "pipeline": "ci",
    "github actions": "ci",
    "workflow": "ci",
    "deploy": "deploy",
    "deployment": "deploy",
    "release": "deploy",
    "production": "deploy",
    "staging": "deploy",
    # security & secrets
    "secret": "security",
    "secrets": "security",
    "credential": "security",
    "credentials": "security",
    "token": "security",
    "api key": "security",
    "password": "security",
    "security": "security",
    "vulnerability": "security",
    "sanitize": "security",
    "injection": "security",
    "permissions": "security",
    # files & fs
    "file": "files",
    "files": "files",
    "directory": "files",
    "folder": "files",
    "path": "files",
    "delete": "files",
    "rename": "files",
    # errors & logging
    "error": "errors",
    "errors": "errors",
    "exception": "errors",
    "exceptions": "errors",
    "logging": "errors",
    "log": "errors",
    "stack trace": "errors",
    "retry": "errors",
    "retries": "errors",
    "timeout": "errors",
    # process / interaction
    "ask": "interaction",
    "confirm": "interaction",
    "confirmation": "interaction",
    "approval": "interaction",
    "permission": "interaction",
    "clarify": "interaction",
    # languages (programming)
    "python": "lang",
    "typescript": "lang",
    "javascript": "lang",
    "rust": "lang",
    "go": "lang",
    "java": "lang",
    "sql": "lang",
    "bash": "lang",
    "shell": "lang",
    # database
    "database": "db",
    "migration": "db",
    "migrations": "db",
    "schema": "db",
    "query": "db",
    # tools/agents
    "mcp": "agent-tools",
    "tool": "agent-tools",
    "tools": "agent-tools",
    "subagent": "agent-tools",
    "skill": "agent-tools",
    "hook": "agent-tools",
}

_MULTIWORD_TOPICS = sorted((k for k in TOPIC_KEYWORDS if " " in k), key=len, reverse=True)


def topics_for(text: str) -> tuple[str, ...]:
    """Topic tags for an instruction text (order-stable, deduped)."""
    low = " " + re.sub(r"[^\w\s%-]", " ", text.lower()) + " "
    found: list[str] = []
    for kw in _MULTIWORD_TOPICS:
        if f" {kw} " in low or f" {kw}." in low:
            tag = TOPIC_KEYWORDS[kw]
            if tag not in found:
                found.append(tag)
    for tok in low.split():
        tag = TOPIC_KEYWORDS.get(tok)
        if tag and tag not in found:
            found.append(tag)
    return tuple(found)


# ---------------------------------------------------------------------------
# Stopwords for object/keyword extraction
# ---------------------------------------------------------------------------

STOPWORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "if",
        "then",
        "else",
        "when",
        "while",
        "for",
        "to",
        "of",
        "in",
        "on",
        "at",
        "by",
        "with",
        "without",
        "from",
        "into",
        "onto",
        "over",
        "under",
        "about",
        "as",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "do",
        "does",
        "did",
        "done",
        "have",
        "has",
        "had",
        "having",
        "will",
        "would",
        "shall",
        "should",
        "may",
        "might",
        "can",
        "could",
        "must",
        "not",
        "no",
        "nor",
        "so",
        "such",
        "that",
        "this",
        "these",
        "those",
        "it",
        "its",
        "it's",
        "your",
        "you",
        "we",
        "our",
        "their",
        "his",
        "her",
        "they",
        "them",
        "there",
        "here",
        "where",
        "how",
        "what",
        "which",
        "who",
        "whom",
        "why",
        "all",
        "any",
        "both",
        "each",
        "few",
        "more",
        "most",
        "other",
        "some",
        "only",
        "own",
        "same",
        "than",
        "too",
        "very",
        "just",
        "also",
        "ever",
        "never",
        "always",
    ]
)


def content_tokens(text: str) -> list[str]:
    """Lowercased content-word tokens (stopwords and punctuation removed)."""
    toks = re.findall(r"[a-zA-Z_][\w./-]*|\d+", text.lower())
    return [t for t in toks if t not in STOPWORDS and len(t) > 1]
