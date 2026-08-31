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

Backend-agnostic: runs on the Anthropic API, the Claude Code CLI in print
mode (your existing subscription, zero configuration), or any
OpenAI-compatible endpoint — see lanes/backends.py.
"""

from __future__ import annotations

import hashlib
import json
import re

from ..config import Config
from ..detectors.base import AnalysisContext
from ..findings import Finding, pair_evidence
from ..ir import UnitPair
from ..similarity import text_similarity
from ..taxonomy import Severity
from . import backends
from .backends import Backend, JuryError
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


class Juror:
    """Backend-agnostic juror: prompts + parsing here, transport in the backend."""

    def __init__(self, backend: Backend):
        self.backend = backend

    @property
    def ident(self) -> str:
        return self.backend.ident

    def judge(self, pair: UnitPair, swapped: bool) -> dict | None:
        text = self.backend.complete(SYSTEM_PROMPT, _pair_prompt(pair, swapped))
        return _parse_verdict(text)


def adjudicate(juror: Juror, pair: UnitPair, cache) -> dict:
    """Swap-stable verdict for one pair, cached. Returns
    {"verdict": ..., "abstained": bool, ...}."""
    key = cache.key(juror.ident, _PROMPT_HASH, f"{pair.key}|swap-both")
    hit = cache.get(key)
    if hit is not None:
        return hit

    try:
        v1 = juror.judge(pair, swapped=False)
        v2 = juror.judge(pair, swapped=True)
    except JuryError as e:
        # transport failure: do not cache, do not abstain-cache a transient
        return {
            "verdict": "DISTINCT",
            "abstained": True,
            "reason": f"backend error: {e}",
            "transient": True,
        }
    result: dict
    conflict_group = {"CONTRADICTORY", "CONDITIONAL_CONFLICT"}
    if v1 is None or v2 is None:
        result = {"verdict": "DISTINCT", "abstained": True, "reason": "unparseable output"}
    elif v1["verdict"] != v2["verdict"] and not (
        v1["verdict"] in conflict_group and v2["verdict"] in conflict_group
    ):
        result = {
            "verdict": "DISTINCT",
            "abstained": True,
            "reason": f"order instability ({v1['verdict']} vs {v2['verdict']})",
        }
    elif v1["verdict"] != v2["verdict"]:
        # both orderings agree a conflict exists but differ on its flavor:
        # take the weaker (conditional) reading with the lower confidence —
        # swap consistency is enforced at the verdict-GROUP level
        weaker = v1 if v1["verdict"] == "CONDITIONAL_CONFLICT" else v2
        if not _evidence_valid(weaker, pair, swapped=weaker is v2):
            result = {
                "verdict": "DISTINCT",
                "abstained": True,
                "reason": "evidence not found in source",
            }
        else:
            result = {
                **weaker,
                "abstained": False,
                "swap_softened": True,
                "confidence": min(
                    float(v1.get("confidence", 0.6)), float(v2.get("confidence", 0.6))
                ),
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


def _verdict_to_code(verdict: str, conflict_type: str) -> str | None:
    """Map (verdict, conflict_type) into the taxonomy: a numeric clash the
    jury judged CONTRADICTORY is a DTC03, not a generic DTC01."""
    if verdict == "CONTRADICTORY":
        return "DTC03" if conflict_type == "numeric" else "DTC01"
    if verdict == "CONDITIONAL_CONFLICT":
        return "DTC03" if conflict_type == "numeric" else "DTC02"
    if verdict == "REDUNDANT":
        return "DTR01"
    return None


def run_jury_lane(cfg: Config, ctx: AnalysisContext, findings: list[Finding]) -> list[Finding]:
    try:
        juror = Juror(backends.make_backend(cfg))
    except JuryError as e:
        ctx.corpus.notes.append(f"{e} — lane skipped")
        return findings

    cache = make_cache(cfg)

    # screen-lane nominations come FIRST: a strong model chose them by
    # reading the whole config, including pairs blocking could never form
    screened: list[UnitPair] = []
    screened_keys: set[str] = set()
    if cfg.lane_screen:
        from .screen import run_screen_lane

        try:
            screen_backend = backends.make_backend(cfg, role="screen")
        except JuryError as e:
            ctx.corpus.notes.append(f"screen lane unavailable ({e}); skipped")
        else:
            screened = run_screen_lane(cfg, ctx, screen_backend)
            screened_keys = {p.key for p in screened}

    # then: NLI bands if the NLI lane ran, else similarity-ranked unclaimed
    # candidate pairs (highest lexical similarity first)
    nli_not_cleared = getattr(ctx, "nli_not_cleared", None)
    if nli_not_cleared is not None:
        rest = [p for p, _ in nli_not_cleared]
    else:
        rest = sorted(
            (
                p
                for p in ctx.pairs
                if not ctx.is_claimed(p) and p.a.is_instruction and p.b.is_instruction
            ),
            key=lambda p: -p.similarity,
        )
    banded = screened + [p for p in rest if p.key not in screened_keys]
    banded = banded[: cfg.jury_max_pairs]

    calls = 0
    transient_failures = 0
    aborted = False
    for pair in banded:
        pair_lanes = (
            ("jury", "screen")
            if any(k.startswith("screen:") for k in pair.block_keys)
            else ("jury",)
        )
        result = adjudicate(juror, pair, cache)
        calls += 1
        if calls % 10 == 0:
            # incremental persistence: a deep run adjudicates hundreds of
            # pairs over hours — a crash must not lose the verdicts so far
            cache.save()
        if result.get("transient"):
            transient_failures += 1
            if transient_failures >= 3:
                ctx.corpus.notes.append(
                    f"jury lane: aborting after {transient_failures} backend failures "
                    f"({result.get('reason', '')})"
                )
                aborted = True
                break
            continue
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
                    lanes=pair_lanes,
                    tags=("needs-human",),
                )
            )
            continue
        code = _verdict_to_code(result["verdict"], str(result.get("conflict_type", "")))
        if code is None:
            continue
        # the deterministic carve-out gate applies to jury verdicts too: a
        # conditional conflict where exactly one side is a deliberate
        # exception ("unless ...", "only when ...") is DTP03-fragile, not a
        # conflict — this is the router's rule, kept consistent here
        if code == "DTC02":
            from ..extract import has_exception_marker

            exc_a = has_exception_marker(pair.a.text)
            exc_b = has_exception_marker(pair.b.text)
            if exc_a != exc_b:
                code = "DTP03"
        # measured on the novel-phrasing holdout (single haiku juror): benign
        # false positives concentrate entirely in CONDITIONAL_CONFLICT — the
        # jury's "maybe" bucket — so those land at advisory, never CI-blocking
        if result["verdict"] == "CONDITIONAL_CONFLICT" or code == "DTP03":
            sev = Severity.ADVISORY
        else:
            sev = Severity.WARNING
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
                lanes=pair_lanes,
            )
        )
    cache.save()
    if not aborted:
        ctx.lanes_ran.add("jury")
    ctx.corpus.notes.append(f"jury lane: adjudicated {calls} pair(s) with {juror.ident}")
    return findings
