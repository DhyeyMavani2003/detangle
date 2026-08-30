"""Shared logic: do two units' prescriptions disagree, and how?

This encodes the deterministic disagreement tests the taxonomy routes on:

- modality clash on a matched (action, object) frame — the Lupu–Sloman
  triple-overlap rule (forbid ∧ oblige = hard; permit ∧ forbid = weaker)
- antonym clash: same action with antonymous objects, or antonymous actions
  on the same object, both prescribed
- numeric clash: same subject/dimension with empty range intersection

All tests are precision-first: unknown fields never match.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from ..ir import InstructionUnit, Modality, Quantity, Strength
from ..lexicons import IMPERATIVE_VERBS, UNIT_DIMENSION, are_antonyms

# supplementary verbs seen in real configs whose frames should still qualify
_EXTRA_VERBS = frozenset(
    [
        "change",
        "browse",
        "share",
        "obey",
        "access",
        "modify",
        "touch",
        "alter",
        "force",
        "hardcode",
        "expose",
        "leak",
        "echo",
        "say",
        "mention",
        "reveal",
        "disclose",
        "overwrite",
        "bypass",
        "suppress",
        "silence",
        "fabricate",
        "invent",
        "guess",
        "hallucinate",
        "assume",
        "commit",
        "push",
    ]
)
_KNOWN_VERBS = IMPERATIVE_VERBS | _EXTRA_VERBS


def _plausible_verb(action: str) -> bool:
    return action in _KNOWN_VERBS


@dataclass
class Disagreement:
    kind: str  # "modality" | "antonym" | "numeric"
    hard: bool  # both sides hard-strength prescriptive
    detail: str  # human-readable account of the clash
    permit_involved: bool = False
    a_quantity: Quantity | None = None
    b_quantity: Quantity | None = None


# object-head equivalences: treat as the same resource
_OBJ_NORMALIZE = {
    "main": "main branch",
    "master": "main branch",
    "main_branch": "main branch",
    "trunk": "main branch",
}


def _obj_head(obj: str) -> str:
    if not obj:
        return ""
    head = obj.split()[0]
    return _OBJ_NORMALIZE.get(head, head)


def _objects_match(a: InstructionUnit, b: InstructionUnit) -> bool:
    oa, ob = a.frame.obj, b.frame.obj
    if not oa or not ob:
        # empty objects never match: on real repos the pairs whose object
        # extraction failed on both sides were overwhelmingly unrelated
        return False
    if oa == ob:
        return True
    ha, hb = _obj_head(oa), _obj_head(ob)
    if ha == hb:
        return True
    # whole-phrase containment only ("main" ⊂ "main hotfixes"); a merely
    # shared head word ("cargo test" vs "test the project") matched
    # refinements like "don't run X directly / run Y instead" on real repos
    wa, wb = oa.split(), ob.split()
    return bool(
        (len(wa) <= len(wb) and wa == wb[: len(wa)]) or (len(wb) <= len(wa) and wb == wa[: len(wb)])
    )


def _objects_antonymous(a: InstructionUnit, b: InstructionUnit) -> bool:
    wa = a.frame.obj.split()
    wb = b.frame.obj.split()
    if not wa or not wb:
        return False
    # antonymous heads with the rest of the phrase compatible
    if are_antonyms(wa[0], wb[0]):
        rest_a, rest_b = " ".join(wa[1:]), " ".join(wb[1:])
        return rest_a == rest_b or not rest_a or not rest_b
    return False


def _actions_match(a: InstructionUnit, b: InstructionUnit) -> bool:
    return bool(
        a.frame.action and a.frame.action == b.frame.action and _plausible_verb(a.frame.action)
    )


def _actions_antonymous(a: InstructionUnit, b: InstructionUnit) -> bool:
    return bool(a.frame.action and b.frame.action and are_antonyms(a.frame.action, b.frame.action))


def _polarity(u: InstructionUnit) -> int:
    """+1 = do it, -1 = don't, 0 = neither (permit/prefer treated separately)."""
    m = u.frame.modality
    if m == Modality.FORBID:
        return -1
    if m == Modality.OBLIGE:
        return 1
    return 0


def _is_hard(u: InstructionUnit) -> bool:
    return u.frame.strength == Strength.HARD


def modality_disagreement(a: InstructionUnit, b: InstructionUnit) -> Disagreement | None:
    """Lupu–Sloman triple overlap: same (action, object), opposing modality."""
    ma, mb = a.frame.modality, b.frame.modality

    # oblige vs forbid on the same action+object
    if _actions_match(a, b) and _objects_match(a, b):
        pa, pb = _polarity(a), _polarity(b)
        if pa * pb == -1:
            return Disagreement(
                kind="modality",
                hard=_is_hard(a) and _is_hard(b),
                detail=(
                    f"one side {'requires' if pa > 0 else 'forbids'} "
                    f"'{a.frame.action}{' ' + a.frame.obj if a.frame.obj else ''}', "
                    f"the other {'requires' if pb > 0 else 'forbids'} it"
                ),
            )
        # permit vs forbid (weaker class per Aires)
        if {ma, mb} == {Modality.PERMIT, Modality.FORBID}:
            return Disagreement(
                kind="modality",
                hard=False,
                permit_involved=True,
                detail=(
                    f"one side permits '{a.frame.action}"
                    f"{' ' + a.frame.obj if a.frame.obj else ''}', the other forbids it"
                ),
            )

    # same action prescribed on mutually exclusive objects
    # ("use tabs" vs "use spaces") — the objects are antonyms, so by
    # definition they do NOT pass the object-match test above
    if _actions_match(a, b) and _objects_antonymous(a, b) and _polarity(a) == _polarity(b) == 1:
        return Disagreement(
            kind="antonym",
            hard=_is_hard(a) and _is_hard(b),
            detail=(
                f"both require action '{a.frame.action}' on mutually exclusive "
                f"objects: '{a.frame.obj}' vs '{b.frame.obj}'"
            ),
        )

    # antonymous actions on the same object ("enable X" vs "disable X")
    if _actions_antonymous(a, b) and _objects_match(a, b) and _polarity(a) == _polarity(b) == 1:
        return Disagreement(
            kind="antonym",
            hard=_is_hard(a) and _is_hard(b),
            detail=(
                f"antonymous prescriptions on the same object: "
                f"'{(a.frame.action + ' ' + a.frame.obj).strip()}' vs "
                f"'{(b.frame.action + ' ' + b.frame.obj).strip()}'"
            ),
        )
    return None


# ---------------------------------------------------------------------------
# Numeric disagreement
# ---------------------------------------------------------------------------


def _interval(q: Quantity) -> tuple[float, float]:
    v = q.value
    return {
        "==": (v, v),
        "<=": (-math.inf, v),
        "<": (-math.inf, v - 1e-9),
        ">=": (v, math.inf),
        ">": (v + 1e-9, math.inf),
        "~": (v * 0.5, v * 1.5),
    }.get(q.comparator, (v, v))


def _dimension(q: Quantity) -> tuple[str, float]:
    if q.unit in UNIT_DIMENSION:
        return UNIT_DIMENSION[q.unit]
    return (q.unit or q.subject or "", 1.0)


_ANCHOR_RE = None


def _anchors(text: str) -> frozenset[str]:
    """Nouns that identify WHICH quantity a sentence constrains."""
    global _ANCHOR_RE
    if _ANCHOR_RE is None:
        import re

        _ANCHOR_RE = re.compile(
            r"\b(timeouts?|intervals?|delays?|retries|retry|attempts?|limits?|budgets?|"
            r"sizes?|lengths?|durations?|waits?|depths?|widths?|caps?|quotas?|"
            r"iterations?|deadlines?)\b",
            re.IGNORECASE,
        )
    return frozenset(m.lower().rstrip("s") for m in _ANCHOR_RE.findall(text))


def _subjects_comparable(qa: Quantity, qb: Quantity, text_a: str = "", text_b: str = "") -> bool:
    if qa.subject and qa.subject == qb.subject:
        # same-dimension units used as bare subjects still need a shared
        # anchor noun: "global timeout 20 min" vs "refetch interval 3s" are
        # different knobs even though both are seconds
        if qa.subject == qa.unit and _dimension(qa)[0] == "time":
            aa, ab = _anchors(text_a), _anchors(text_b)
            return not aa or not ab or bool(aa & ab)
        return True
    da, db = _dimension(qa), _dimension(qb)
    if da[0] and da[0] == db[0] and da[0] != "text":
        # cross-unit comparison ("30 seconds" vs "2 minutes") requires the
        # two sentences to constrain the same named thing
        aa, ab = _anchors(text_a), _anchors(text_b)
        return bool(aa & ab)
    return False


def quantities_conflict(a: InstructionUnit, b: InstructionUnit) -> Disagreement | None:
    """Empty intersection of numeric ranges about the same subject.

    Precision gates (added after real-repo dogfooding):
    - a quantity inside a unit's *condition* clause is a trigger threshold,
      not a prescription ("split anything over 800 lines" vs "keep files
      under 500 lines" is a band, not a conflict) — skipped;
    - unless the two units share the same action verb, at least one
      comparator must be '==' (opposite-direction bounds in sentences that
      merely share a subject are usually target-vs-trigger bands);
    - bare 'percent' quantities are only comparable when actions match.
    """
    actions_same = _actions_match(a, b)
    for qa in a.quantities:
        for qb in b.quantities:
            if not _subjects_comparable(qa, qb, a.text, b.text):
                continue
            if qa.raw and qa.raw in a.frame.condition:
                continue
            if qb.raw and qb.raw in b.frame.condition:
                continue
            if not actions_same:
                if "==" not in (qa.comparator, qb.comparator):
                    continue
                if qa.subject == "percent" and qa.unit == "percent":
                    continue
            da, db = _dimension(qa), _dimension(qb)
            lo_a, hi_a = _interval(qa)
            lo_b, hi_b = _interval(qb)
            if da[0] == db[0] and da[0]:
                lo_a, hi_a = lo_a * da[1], hi_a * da[1]
                lo_b, hi_b = lo_b * db[1], hi_b * db[1]
            if max(lo_a, lo_b) > min(hi_a, hi_b):
                return Disagreement(
                    kind="numeric",
                    hard=True,
                    detail=(
                        f"'{qa.raw}' and '{qb.raw}' cannot both hold (ranges do not intersect)"
                    ),
                    a_quantity=qa,
                    b_quantity=qb,
                )
    return None


# ---------------------------------------------------------------------------
# Pragmatic (tone) tension — DTC08 territory
# ---------------------------------------------------------------------------

_CONCISE_RE = None
_VERBOSE_RE = None


def _tone_res():
    global _CONCISE_RE, _VERBOSE_RE
    if _CONCISE_RE is None:
        import re

        _CONCISE_RE = re.compile(
            r"\b(concise(?:ly)?|brief(?:ly)?|terse|succinct(?:ly)?|to\s+the\s+point|"
            r"minimal\s+(?:prose|output|text)|short\s+(?:replies|responses|answers))\b",
            re.IGNORECASE,
        )
        _VERBOSE_RE = re.compile(
            r"\b(in\s+(?:great\s+)?detail|detailed(?:\s+\w+)?|verbose(?:ly)?|thorough(?:ly)?|"
            r"comprehensive(?:ly)?|elaborate|step[\s-]by[\s-]step|explain\s+(?:your\s+)?"
            r"(?:reasoning|thinking|every))\b",
            re.IGNORECASE,
        )
    return _CONCISE_RE, _VERBOSE_RE


def pragmatic_tension(a: InstructionUnit, b: InstructionUnit) -> Disagreement | None:
    """'Be concise' vs 'always explain in detail': jointly satisfiable but
    mutually degrading. Only fires when both units are about output/tone."""
    concise_re, verbose_re = _tone_res()
    ca, va = bool(concise_re.search(a.text)), bool(verbose_re.search(a.text))
    cb, vb = bool(concise_re.search(b.text)), bool(verbose_re.search(b.text))
    hit = (ca and vb and not va and not cb) or (cb and va and not vb and not ca)
    if not hit:
        return None
    if not ({"output", "output-format"} & set(a.topics + b.topics)):
        # require at least one side to clearly be about responses/output
        return None
    return Disagreement(
        kind="tension",
        hard=False,
        detail=(
            f"'{a.short(60)}' and '{b.short(60)}' pull the same output dimension in "
            "opposite directions"
        ),
    )


def find_disagreement(a: InstructionUnit, b: InstructionUnit) -> Disagreement | None:
    """The main entry: numeric first (highest precision), then modality/antonym,
    then soft tone tension."""
    d = quantities_conflict(a, b)
    if d:
        return d
    d = modality_disagreement(a, b)
    if d:
        return d
    return pragmatic_tension(a, b)
