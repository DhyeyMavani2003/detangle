"""LLM jury lane: schema-constrained adjudication of candidate pairs.

Implements the Jury Protocol (single-juror tier in v0.1; the protocol shape
is the multi-juror one so more jurors are additive):

- The judge only ADJUDICATES pre-extracted pairs, never discovers conflicts
  in raw text (open-ended detection collapses to 8% recall).
- Neutral framing ("classify the relationship"), never "confirm this
  conflict" (sycophancy).
- JSON with evidence fields strictly BEFORE the verdict token; short
  reasoning summary, no long chain-of-thought (hurts calibration).
- Order-swap rule: each pair is judged as (A,B) and (B,A); differing
  verdicts ⇒ the juror abstains ⇒ NEEDS_HUMAN at info level, never a
  CI-failing severity.
- Verdict cache keyed (linter_version, prompt_hash, model, pair_hash,
  ordering_policy). Determinism comes from caching + enums + swap, never
  from temperature=0.
- Evidence validation: quoted spans must actually occur in the source
  texts, else the verdict is rejected.

Requires `detangle[jury]` and ANTHROPIC_API_KEY.
"""

from __future__ import annotations

import hashlib
import json
import os
import re

from ..config import Config
from ..detectors.base import AnalysisContext
from ..findings import Finding, pair_evidence
from ..ir import UnitPair
from ..similarity import text_similarity
from ..taxonomy import Severity
from .cachekey import make_cache

VERDICTS = (
    "CONTRADICTORY",
    "CONDITIONAL_CONFLICT",
    "PRECEDENCE_RESOLVED",
    "REDUNDANT",
    "DISTINCT",
)
CONFLICT_TYPES = (
    "negation",
    "unsatisfiable_constraint",
    "temporal",
    "numeric",
    "specificity",
    "authority",
    "process",
    "none",
)

SYSTEM_PROMPT = """You are a careful analyst of natural-language agent configurations. \
You will be shown two instructions (with file, line, layer, activation-scope and \
modality metadata) that can be simultaneously active in an AI agent's context. \
Classify the relationship between them. Do not assume they conflict; most pairs do not.

Respond with ONLY a JSON object, no other text, with EXACTLY these fields in this order:
{
  "overlap_condition": "<when both instructions apply simultaneously; '' if never>",
  "evidence_a": "<the exact phrase quoted verbatim from instruction A that drives your verdict>",
  "evidence_b": "<the exact phrase quoted verbatim from instruction B that drives your verdict>",
  "reasoning_summary": "<at most 40 words>",
  "verdict": "<one of: CONTRADICTORY | CONDITIONAL_CONFLICT | PRECEDENCE_RESOLVED | REDUNDANT | DISTINCT>",
  "conflict_type": "<one of: negation | unsatisfiable_constraint | temporal | numeric | specificity | authority | process | none>",
  "resolution_hint": "<one sentence; '' if verdict is DISTINCT>",
  "confidence": <0.0-1.0>
}

Verdict meanings:
- CONTRADICTORY: incompatible prescriptions for the same situation.
- CONDITIONAL_CONFLICT: individually satisfiable; jointly unsatisfiable when the overlap_condition holds.
- PRECEDENCE_RESOLVED: they disagree, but the provided layer/tier metadata declares which one wins.
- REDUNDANT: same prescription stated twice (entailment both ways).
- DISTINCT: compatible or unrelated."""

_PROMPT_HASH = hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()[:12]


def _pair_prompt(pair: UnitPair, swapped: bool) -> str:
    a, b = (pair.b, pair.a) if swapped else (pair.a, pair.b)

    def block(label: str, u) -> str:
        return (
            f"Instruction {label}:\n"
            f"  text: {json.dumps(u.text)}\n"
            f"  file: {u.file.path} (lines {u.span.start_line}-{u.span.end_line})\n"
            f"  layer: {u.file.mechanism}/{u.layer.value}, tier {u.tier}\n"
            f"  activation: {u.activation.mode.value}"
            + (f" globs={list(u.activation.globs)}" if u.activation.globs else "")
            + (f"\n  condition: {u.frame.condition}" if u.frame.condition else "")
            + f"\n  modality: {u.frame.modality.value} ({u.frame.strength.value})"
        )

    return (
        block("A", a)
        + "\n\n"
        + block("B", b)
        + "\n\nCo-activation: "
        + pair.co_activation_account
        + "\nDeclared precedence: "
        + (pair.precedence.account or "none")
        + "\n\nClassify the relationship. JSON only."
    )


def _parse_verdict(raw: str) -> dict | None:
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if data.get("verdict") not in VERDICTS:
        return None
    return data


def _evidence_valid(data: dict, pair: UnitPair, swapped: bool) -> bool:
    a, b = (pair.b, pair.a) if swapped else (pair.a, pair.b)
    ea = str(data.get("evidence_a", ""))
    eb = str(data.get("evidence_b", ""))

    def occurs(quote: str, text: str) -> bool:
        if not quote:
            return False
        q = " ".join(quote.split()).lower().strip(".\"'")
        t = " ".join(text.split()).lower()
        return q in t or text_similarity(quote, text) > 0.5

    return occurs(ea, a.text) and occurs(eb, b.text)


class JuryError(RuntimeError):
    pass


class AnthropicJuror:
    def __init__(self, model: str):
        try:
            import anthropic  # type: ignore[import-not-found]
        except ImportError as e:
            raise JuryError(
                "jury lane requires the anthropic package — install `detangle[jury]`"
            ) from e
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise JuryError("jury lane requires ANTHROPIC_API_KEY in the environment")
        self.client = anthropic.Anthropic()
        self.model = model

    def judge(self, pair: UnitPair, swapped: bool) -> dict | None:
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=400,
            temperature=0,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _pair_prompt(pair, swapped)}],
        )
        text = "".join(getattr(b, "text", "") for b in resp.content)
        return _parse_verdict(text)


def adjudicate(juror: AnthropicJuror, pair: UnitPair, cache) -> dict:
    """Swap-stable verdict for one pair, cached. Returns
    {"verdict": ..., "abstained": bool, ...}."""
    key = cache.key(juror.model, _PROMPT_HASH, f"{pair.key}|swap-both")
    hit = cache.get(key)
    if hit is not None:
        return hit

    v1 = juror.judge(pair, swapped=False)
    v2 = juror.judge(pair, swapped=True)
    result: dict
    if v1 is None or v2 is None:
        result = {"verdict": "DISTINCT", "abstained": True, "reason": "unparseable output"}
    elif v1["verdict"] != v2["verdict"]:
        result = {
            "verdict": "DISTINCT",
            "abstained": True,
            "reason": f"order instability ({v1['verdict']} vs {v2['verdict']})",
        }
    elif not _evidence_valid(v1, pair, swapped=False):
        result = {
            "verdict": "DISTINCT",
            "abstained": True,
            "reason": "evidence not found in source",
        }
    else:
        result = {**v1, "abstained": False}
    cache.put(key, result)
    return result


_VERDICT_TO_CODE = {
    "CONTRADICTORY": "DTC01",
    "CONDITIONAL_CONFLICT": "DTC02",
    "REDUNDANT": "DTR01",
}


def run_jury_lane(cfg: Config, ctx: AnalysisContext, findings: list[Finding]) -> list[Finding]:
    try:
        juror = AnthropicJuror(cfg.jury_model)
    except JuryError as e:
        ctx.corpus.notes.append(f"{e} — lane skipped")
        return findings

    cache = make_cache(cfg)

    # candidates: NLI bands if the NLI lane ran, else similarity-ranked
    # unclaimed pairs (highest lexical similarity first)
    banded: list[UnitPair] = []
    nli_flag = getattr(ctx, "nli_flag", None)
    nli_gray = getattr(ctx, "nli_gray", None)
    if nli_flag is not None or nli_gray is not None:
        banded = [p for p, _ in (nli_flag or [])] + [p for p, _ in (nli_gray or [])]
    else:
        banded = sorted(
            (p for p in ctx.pairs if not ctx.is_claimed(p)),
            key=lambda p: -p.similarity,
        )
    banded = banded[: cfg.jury_max_pairs]

    calls = 0
    for pair in banded:
        result = adjudicate(juror, pair, cache)
        calls += 1
        if result.get("abstained"):
            findings.append(
                Finding(
                    code="DTC02",
                    message=(
                        "NEEDS_HUMAN: the jury could not produce a stable verdict "
                        f"({result.get('reason', 'abstained')})."
                    ),
                    severity=Severity.INFO,
                    evidence=pair_evidence(pair),
                    units=[pair.a, pair.b],
                    co_activation=pair.co_activation_account,
                    precedence=pair.precedence.account,
                    confidence=0.3,
                    lanes=("jury",),
                    tags=("needs-human",),
                )
            )
            continue
        code = _VERDICT_TO_CODE.get(result["verdict"])
        if code is None:
            continue
        sev = Severity.WARNING if code.startswith("DTC") else Severity.ADVISORY
        findings.append(
            Finding(
                code=code,
                message=(
                    f"Jury verdict {result['verdict']}"
                    + (
                        f" ({result.get('conflict_type')})"
                        if result.get("conflict_type") not in (None, "none")
                        else ""
                    )
                    + f": {result.get('reasoning_summary', '').strip()}"
                ),
                severity=sev,
                evidence=pair_evidence(pair),
                units=[pair.a, pair.b],
                co_activation=pair.co_activation_account,
                precedence=pair.precedence.account,
                suggestion=str(result.get("resolution_hint", "") or ""),
                witness=str(result.get("overlap_condition", "") or ""),
                confidence=float(result.get("confidence", 0.6)),
                lanes=("jury",),
            )
        )
    cache.save()
    ctx.corpus.notes.append(f"jury lane: adjudicated {calls} pairs with {cfg.jury_model}")
    return findings
