"""Instruction-unit extraction: sentences -> frames, quantities, normalization.

This is the deterministic (zero-LLM) extraction lane. It is deliberately
conservative: fields it cannot infer stay empty, and downstream detectors
treat empty as "unknown", never as "matching". The imperative→declarative
normalization exists because NLI models are trained on declaratives and
because content-hash identity should survive trivial rephrasing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .ir import (
    Activation,
    ConfigFile,
    Frame,
    InstructionUnit,
    Modality,
    Quantity,
    SourceSpan,
    Strength,
)
from .lexicons import (
    COMPARATOR_PHRASES,
    CONDITION_LEADERS,
    ESCAPE_CLAUSE_RE,
    EXCEPTION_MARKERS_RE,
    FORBID_HARD,
    FORBID_SOFT,
    IMPERATIVE_VERBS,
    NON_IMPERATIVE_STARTERS,
    OBLIGE_HARD,
    OBLIGE_SOFT,
    PERMIT,
    PREFER,
    STOPWORDS,
    UNIT_ALIASES,
    WORD_NUMBERS,
    topics_for,
)
from .markdown import Sentence, iter_sentences, parse_blocks


@dataclass
class _ModalityHit:
    modality: Modality
    strength: Strength
    pattern: str
    pos: int


def _compile(pats: tuple[str, ...]) -> list[re.Pattern[str]]:
    return [re.compile(p, re.IGNORECASE) for p in pats]


_MODALITY_TABLE: list[tuple[list[re.Pattern[str]], Modality, Strength]] = [
    (_compile(FORBID_HARD), Modality.FORBID, Strength.HARD),
    (_compile(FORBID_SOFT), Modality.FORBID, Strength.SOFT),
    (_compile(OBLIGE_HARD), Modality.OBLIGE, Strength.HARD),
    (_compile(OBLIGE_SOFT), Modality.OBLIGE, Strength.SOFT),
    (_compile(PERMIT), Modality.PERMIT, Strength.SOFT),
    (_compile(PREFER), Modality.PREFER, Strength.SOFT),
]


def detect_modality(text: str) -> _ModalityHit | None:
    """Earliest-match wins across all tables; ties broken by table order."""
    best: _ModalityHit | None = None
    for patterns, modality, strength in _MODALITY_TABLE:
        for pat in patterns:
            m = pat.search(text)
            if m and (best is None or m.start() < best.pos):
                best = _ModalityHit(modality, strength, pat.pattern, m.start())
    return best


_LEADING_CONDITION_RE = re.compile(
    r"^(?P<cond>(?:" + "|".join(CONDITION_LEADERS) + r")\b[^,;]{2,80})[,;:]\s+(?P<rest>.+)$",
    re.IGNORECASE,
)
_TRAILING_CONDITION_RE = re.compile(
    r"^(?P<rest>.{8,}?)[,;]?\s+(?P<cond>(?:unless|except\s+when|except\s+if|when|whenever|if|while)\b[^.]{2,80})[.]?$",
    re.IGNORECASE,
)


def split_condition(text: str) -> tuple[str, str]:
    """Return (body, condition_text). Condition may be ''."""
    m = _LEADING_CONDITION_RE.match(text.strip())
    if m:
        return m.group("rest").strip(), m.group("cond").strip()
    m = _TRAILING_CONDITION_RE.match(text.strip())
    if m:
        cond = m.group("cond").strip()
        # only treat as condition if introduced by a real subordinator
        if re.match(r"(unless|except|when|whenever|if|while)\b", cond, re.IGNORECASE):
            return m.group("rest").strip(), cond
    return text.strip(), ""


# ---------------------------------------------------------------------------
# Quantities
# ---------------------------------------------------------------------------

_NUM_RE = r"(?P<num>\d+(?:[.,]\d+)?|" + "|".join(re.escape(w) for w in WORD_NUMBERS) + r")"
_UNIT_RE = r"(?P<unit>[a-zA-Z%]+)?"
_CMP_ALTS = "|".join(p for p, _ in COMPARATOR_PHRASES)
_QTY_RE = re.compile(
    r"(?:(?P<cmp>" + _CMP_ALTS + r"|<=|>=|<|>|==?)\s*)?" + _NUM_RE + r"\s*" + _UNIT_RE,
    re.IGNORECASE,
)
_CMP_LOOKUP = [(re.compile("^(?:" + p + ")$", re.IGNORECASE), c) for p, c in COMPARATOR_PHRASES]


def _normalize_cmp(raw: str | None) -> str:
    if not raw:
        return "=="
    raw = raw.strip()
    if raw in {"<=", ">=", "<", ">", "=", "=="}:
        return "==" if raw == "=" else raw
    for pat, c in _CMP_LOOKUP:
        if pat.match(raw):
            return c
    return "=="


def _parse_number(tok: str) -> float | None:
    tok = tok.lower().replace(",", "")
    if tok in WORD_NUMBERS:
        return float(WORD_NUMBERS[tok])
    try:
        return float(tok)
    except ValueError:
        return None


_QTY_SUBJECT_BLACKLIST = frozenset(
    ["the", "a", "an", "of", "per", "and", "or", "to", "in", "for", "with", "is", "are"]
)


def extract_quantities(text: str) -> list[Quantity]:
    """Extract numeric constraints, e.g. 'at most 3 retries' -> (<=, 3, retries)."""
    out: list[Quantity] = []
    for m in _QTY_RE.finditer(text):
        num = _parse_number(m.group("num"))
        if num is None:
            continue
        raw_unit = (m.group("unit") or "").lower().strip(".,;:")
        unit = UNIT_ALIASES.get(raw_unit, "")
        subject = unit
        if not subject:
            # look ahead a couple of words for the subject noun
            after = text[m.end() :].strip()
            words = re.findall(r"[a-zA-Z_-]+", after)[:3]
            for w in words:
                lw = w.lower()
                if lw in _QTY_SUBJECT_BLACKLIST or lw in STOPWORDS:
                    continue
                subject = UNIT_ALIASES.get(lw, lw)
                break
        # version-like or date-like numbers are not constraints
        ctx = text[max(0, m.start() - 12) : m.end() + 4].lower()
        if re.search(r"\bv(?:ersion)?\s*$", text[: m.start()].lower()[-9:]) or re.search(
            r"\d+\.\d+\.\d+", ctx
        ):
            continue
        cmp_ = _normalize_cmp(m.group("cmp"))
        out.append(
            Quantity(
                value=num,
                unit=unit,
                comparator=cmp_,
                subject=subject,
                raw=m.group(0).strip(),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Frame extraction (action / object / negation)
# ---------------------------------------------------------------------------

_MODAL_STRIP_RE = re.compile(
    r"^(?:you\s+|the\s+agent\s+|claude\s+|please\s+|remember\s+to\s+|note\s+that\s+)*"
    r"(?:always\s+|never\s+|must\s+(?:not\s+)?|do\s+not\s+|don['’]t\s+|shall\s+(?:not\s+)?|"
    r"should\s+(?:not\s+)?|may\s+(?:not\s+)?|can\s+|avoid\s+|refrain\s+from\s+|prefer\s+(?:to\s+)?|"
    r"try\s+(?:not\s+)?to\s+|make\s+sure\s+(?:to\s+|that\s+)?|be\s+sure\s+to\s+|ensure\s+(?:that\s+)?|"
    r"feel\s+free\s+to\s+|it['’]?s\s+(?:ok|okay|fine)\s+to\s+|aim\s+to\s+|need\s+to\s+|needs\s+to\s+|"
    r"have\s+to\s+|are\s+(?:not\s+)?(?:required|allowed|permitted)\s+to\s+)*",
    re.IGNORECASE,
)

_VERB_NORMALIZE = {
    "running": "run",
    "ran": "run",
    "runs": "run",
    "using": "use",
    "uses": "use",
    "used": "use",
    "writing": "write",
    "writes": "write",
    "wrote": "write",
    "written": "write",
    "adding": "add",
    "adds": "add",
    "added": "add",
    "committing": "commit",
    "commits": "commit",
    "committed": "commit",
    "pushing": "push",
    "pushes": "push",
    "pushed": "push",
    "creating": "create",
    "creates": "create",
    "created": "create",
    "deleting": "delete",
    "deletes": "delete",
    "deleted": "delete",
    "including": "include",
    "includes": "include",
    "included": "include",
    "asking": "ask",
    "asks": "ask",
    "asked": "ask",
    "making": "make",
    "makes": "make",
    "made": "make",
}


def extract_frame(body: str, hit: _ModalityHit | None) -> Frame:
    frame = Frame()
    if hit:
        frame.modality = hit.modality
        frame.strength = hit.strength
        frame.negated = hit.modality == Modality.FORBID
    stripped = _MODAL_STRIP_RE.sub("", body.strip(), count=1).strip()
    # "no <gerund>" pattern
    m = re.match(r"^no\s+(\w+ing)\b\s*(.*)$", stripped, re.IGNORECASE)
    if m:
        frame.modality = Modality.FORBID
        frame.negated = True
        stripped = m.group(1) + " " + m.group(2)
    words = re.findall(r"[a-zA-Z_'’./-]+|\S", stripped)
    if not words:
        return frame
    verb = words[0].lower().strip(".,;:")
    frame.raw_verb = verb
    frame.action = _VERB_NORMALIZE.get(verb, verb)
    # object: next few content words (skip adverbs and connector prepositions,
    # stop at temporal/conditional connectives)
    _connectors = {
        "to",
        "in",
        "into",
        "on",
        "onto",
        "for",
        "of",
        "with",
        "at",
        "from",
        "the",
        "a",
        "an",
        "your",
        "any",
        "all",
    }
    _stoppers = {
        "before",
        "after",
        "when",
        "while",
        "unless",
        "if",
        "except",
        "so",
        "because",
        "since",
    }
    obj_words: list[str] = []
    for w in words[1:10]:
        lw = w.lower().strip(".,;:!?\"'`)")
        if not lw:
            break
        if lw in _stoppers:
            break
        if lw.endswith("ly") and len(lw) > 4:
            continue
        if lw in _connectors:
            continue
        if not re.match(r"^[\w./-]+$", lw):
            break
        obj_words.append(lw)
        if len(obj_words) >= 3:
            break
    frame.obj = " ".join(obj_words)
    return frame


# ---------------------------------------------------------------------------
# Instruction detection & normalization
# ---------------------------------------------------------------------------


def looks_like_instruction(text: str, from_bullet: bool) -> bool:
    """Is this sentence prescriptive (vs descriptive/contextual)?"""
    t = text.strip()
    if len(t) < 4:
        return False
    words = re.findall(r"[a-zA-Z'’]+", t)
    if not words:
        return False
    first = words[0].lower()
    if first in NON_IMPERATIVE_STARTERS:
        return False
    if detect_modality(t) is not None:
        return True
    if first in IMPERATIVE_VERBS:
        return True
    # bullet items starting with a bare verb-ish word are usually directives
    if from_bullet and first.endswith(("ify", "ise", "ize")) and len(words) > 1:
        return True
    # second-person directives ("You write tests first.")
    return bool(len(words) > 2 and first == "you" and words[1].lower() in IMPERATIVE_VERBS)


def normalize_declarative(text: str, frame: Frame) -> str:
    """Imperative -> declarative with a fixed subject template.

    'Never push to main.' -> 'The agent must not push to main.'
    Used for NLI inputs and for content-hash identity.
    """
    t = " ".join(text.split()).rstrip(".!").strip()
    if not t:
        return t
    lower = t.lower()
    if lower.startswith(("the agent ", "the assistant ")):
        return t if t.endswith(".") else t + "."
    body, cond = split_condition(t)
    stripped = _MODAL_STRIP_RE.sub("", body, count=1).strip()
    if not stripped:
        stripped = body
    stripped = stripped[0].lower() + stripped[1:] if stripped else stripped
    if frame.modality == Modality.FORBID:
        verb_phrase = "must not" if frame.strength == Strength.HARD else "should not"
    elif frame.modality == Modality.PERMIT:
        verb_phrase = "may"
    elif frame.modality == Modality.PREFER or frame.strength == Strength.SOFT:
        verb_phrase = "should"
    else:
        verb_phrase = "must"
    out = f"The agent {verb_phrase} {stripped}"
    if cond:
        out += f" {cond[0].lower()}{cond[1:]}"
    return out.rstrip(".") + "."


# ---------------------------------------------------------------------------
# Defined terms (DTR03): '"X" means Y', 'X: Y' definition patterns
# ---------------------------------------------------------------------------

_DEFINITION_RES = [
    re.compile(
        r"[\"“'`]?(?P<term>[A-Za-z][\w\s-]{1,30}?)[\"”'`]?\s+(?:means|refers\s+to|is\s+defined\s+as|stands\s+for)\s+(?P<def>.+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(?:we\s+call|call)\s+[\"“'`]?(?P<term>[A-Za-z][\w\s-]{1,30}?)[\"”'`]?\s+(?P<def>.+)",
        re.IGNORECASE,
    ),
]


def extract_defined_terms(text: str) -> tuple[str, ...]:
    terms: list[str] = []
    for rx in _DEFINITION_RES:
        m = rx.search(text)
        if m:
            term = " ".join(m.group("term").split()).lower()
            if 2 <= len(term) <= 32 and term not in terms:
                terms.append(term)
    return tuple(terms)


# ---------------------------------------------------------------------------
# Top-level: file -> units
# ---------------------------------------------------------------------------


def extract_units(file: ConfigFile, body_start_line: int = 1) -> list[InstructionUnit]:
    """Extract instruction units from a parsed config file's text."""
    blocks = parse_blocks(file.text, start_line=body_start_line)
    units: list[InstructionUnit] = []
    for sent in iter_sentences(blocks):
        units.extend(_sentence_to_units(sent, file))
    return units


def _sentence_to_units(sent: Sentence, file: ConfigFile) -> list[InstructionUnit]:
    text = sent.text.strip()
    is_instr = looks_like_instruction(text, sent.from_bullet)
    defined = extract_defined_terms(text)
    if not is_instr and not defined:
        return []
    body, cond = split_condition(text)
    hit = detect_modality(text)
    frame = extract_frame(body, hit)
    frame.condition = cond
    if ESCAPE_CLAUSE_RE.search(text):
        frame.strength = Strength.SOFT
    quantities = extract_quantities(text)
    activation = _refine_activation(file.activation, cond)
    unit = InstructionUnit(
        text=text,
        normalized=normalize_declarative(text, frame),
        span=SourceSpan(file.path, sent.start_line, sent.end_line),
        file=file,
        activation=activation,
        frame=frame,
        quantities=quantities,
        topics=topics_for(text + " " + " > ".join(sent.heading_path)),
        heading=" > ".join(sent.heading_path),
        is_instruction=is_instr,
        defined_terms=defined,
    )
    return [unit]


def _refine_activation(file_act: Activation, condition: str) -> Activation:
    """Unit inherits its file's activation; a textual condition narrows it."""
    return Activation(
        mode=file_act.mode,
        globs=file_act.globs,
        description=file_act.description,
        budget_risk=file_act.budget_risk,
        budget_note=file_act.budget_note,
    )


def has_exception_marker(text: str) -> bool:
    return bool(EXCEPTION_MARKERS_RE.search(text))
