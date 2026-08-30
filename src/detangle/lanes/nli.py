"""NLI lane: cross-encoder contradiction scoring over candidate pairs.

Research-grounded role: NLI is a *recall filter*, never a verdict-giver
(37% precision on norm pairs when used alone). Standalone (without the
jury lane) it therefore only:

- boosts confidence of findings the deterministic lane already made, and
- surfaces NEW pairs as findings only above a very strict symmetrized
  contradiction score, tagged 'nli' at WARNING severity with the
  negation-bias guard applied (never/don't-dense pairs are down-weighted
  per Poliak 2018 / Hossain 2020).

With the jury lane enabled it instead feeds the gray zone to the jury
(Fellegi–Sunter banding).

Model: cross-encoder/nli-deberta-v3-small by default (0.1B pre-filter
tier); configurable. Requires the `detangle[nli]` extra.
"""

from __future__ import annotations

import re

from ..config import Config
from ..detectors.base import AnalysisContext
from ..findings import Finding, pair_evidence
from ..ir import UnitPair
from ..taxonomy import Severity

DEFAULT_NLI_MODEL = "cross-encoder/nli-deberta-v3-small"

# banding thresholds on symmetrized contradiction probability; calibrated on
# the seeded benchmark (benchmarks/), see docs/lanes.md
TAU_LOW = 0.25
TAU_HIGH = 0.88

_NEGATION_RE = re.compile(r"\b(never|not|don['’]t|no|cannot|can['’]t)\b", re.IGNORECASE)


class NliScorer:
    """Lazy wrapper; import fails only when the lane actually runs."""

    def __init__(self, model_name: str = DEFAULT_NLI_MODEL):
        from sentence_transformers import CrossEncoder  # type: ignore[import-not-found]

        self.model = CrossEncoder(model_name)
        # label order for nli-deberta-v3 models
        self.labels = ("contradiction", "entailment", "neutral")

    def contradiction_scores(self, pairs: list[tuple[str, str]]) -> list[float]:
        """Symmetrized max contradiction probability per (premise, hypothesis)."""
        import numpy as np  # transitively available with torch

        inputs: list[tuple[str, str]] = []
        for a, b in pairs:
            inputs.append((a, b))
            inputs.append((b, a))
        logits = self.model.predict(inputs, apply_softmax=True)
        idx = self.labels.index("contradiction")
        probs = np.asarray(logits)[:, idx]
        return [float(max(probs[2 * i], probs[2 * i + 1])) for i in range(len(pairs))]


def gray_zone_pairs(
    ctx: AnalysisContext, scorer: NliScorer
) -> tuple[list[tuple[UnitPair, float]], list[tuple[UnitPair, float]]]:
    """Score unclaimed candidate pairs; return (gray zone, auto-flag band)."""
    unclaimed = [p for p in ctx.pairs if not ctx.is_claimed(p)]
    if not unclaimed:
        return [], []
    scores = scorer.contradiction_scores([(p.a.normalized, p.b.normalized) for p in unclaimed])
    gray: list[tuple[UnitPair, float]] = []
    flag: list[tuple[UnitPair, float]] = []
    for p, s in zip(unclaimed, scores, strict=True):
        if s >= TAU_HIGH:
            flag.append((p, s))
        elif s >= TAU_LOW:
            gray.append((p, s))
    return gray, flag


def _negation_dense(pair: UnitPair) -> bool:
    return bool(_NEGATION_RE.search(pair.a.text) and _NEGATION_RE.search(pair.b.text))


def run_nli_lane(cfg: Config, ctx: AnalysisContext, findings: list[Finding]) -> list[Finding]:
    try:
        scorer = NliScorer(cfg.nli_model)
    except ImportError:
        ctx.corpus.notes.append(
            "NLI lane requested but sentence-transformers is not installed — "
            "install `detangle[nli]`; lane skipped"
        )
        return findings
    except Exception as e:  # model download/load failure
        ctx.corpus.notes.append(f"NLI lane unavailable ({e!r}); lane skipped")
        return findings

    gray, flag = gray_zone_pairs(ctx, scorer)

    # confidence annotation on existing pairwise findings
    by_key = {}
    for f in findings:
        if len(f.units) == 2:
            u, v = sorted((f.units[0].uid, f.units[1].uid))
            by_key[f"{u}:{v}"] = f
    for p, _score in flag:
        f = by_key.get(p.key)
        if f is not None:
            f.lanes = tuple(sorted({*f.lanes, "nli"}))

    if cfg.lane_jury:
        # jury lane consumes the bands via ctx; stash them
        ctx.corpus.notes.append(f"NLI banding: {len(flag)} auto-flag, {len(gray)} gray-zone pairs")
        ctx.nli_gray = gray
        ctx.nli_flag = flag
        return findings

    # standalone: surface only the strict auto-flag band as new findings
    for p, s in flag:
        if p.key in by_key:
            continue
        confidence = 0.55 if _negation_dense(p) else 0.7
        findings.append(
            Finding(
                code="DTC01",
                message=(
                    "NLI cross-encoder scores these as contradictory "
                    f"(symmetrized p={s:.2f}); the deterministic lane could not "
                    "confirm — treat as a lead, not a verdict."
                ),
                severity=Severity.WARNING,
                evidence=pair_evidence(p),
                units=[p.a, p.b],
                co_activation=p.co_activation_account,
                precedence=p.precedence.account,
                suggestion="Review the pair; enable the jury lane to adjudicate automatically.",
                confidence=confidence,
                lanes=("nli",),
            )
        )
    return findings
