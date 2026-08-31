"""NLI lane: cross-encoder scoring over candidate pairs — an AUTO-CLEAR band.

Research-grounded role: NLI is a *recall filter*, never a verdict-giver
(~37% precision on norm pairs when used alone). We validated this
empirically on cross-encoder/nli-deberta-v3-small with declarativized
instruction pairs (2026-08-30, this repo, three normalization templates
A/B-tested per the research's advice):

====================  ==================  ==========
pair type             contradiction prob  usable as
====================  ==================  ==========
true contradiction    ~1.00               —
UNRELATED rules       ~0.99 (!)           —
paraphrase            ~0.00               clear
benign specialization ~0.00               clear
====================  ==================  ==========

The single-event NLI artifact makes ANY two different prescriptions score
as "contradiction", under every template we tried — so a high score cannot
FLAG a pair. A LOW symmetrized contradiction score, however, is a reliable
COMPATIBLE signal. The lane therefore implements only the Fellegi–Sunter
bands that actually exist for this model class:

- **auto-clear** (score < TAU_CLEAR): definitively compatible; with the
  jury enabled these pairs are never sent for adjudication (cost saver).
- everything else is "not cleared": standalone it annotates nothing new
  (no finding is ever emitted from an NLI score alone); with the jury
  enabled the not-cleared pairs are ranked by score and fed to the jury.

Model: cross-encoder/nli-deberta-v3-small by default (0.1B pre-filter
tier); configurable via ``[detangle.nli] model``. Requires the
`detangle[nli]` extra.
"""

from __future__ import annotations

from ..config import Config
from ..detectors.base import AnalysisContext
from ..findings import Finding
from ..ir import UnitPair

DEFAULT_NLI_MODEL = "cross-encoder/nli-deberta-v3-small"

# below this symmetrized contradiction probability a pair is definitively
# compatible (measured: paraphrases/specializations score ~0.00, anything
# prescriptively different scores ~0.99 — the bands are far apart)
TAU_CLEAR = 0.25


class NliScorer:
    """Lazy wrapper; import fails only when the lane actually runs."""

    def __init__(self, model_name: str = DEFAULT_NLI_MODEL):
        from sentence_transformers import CrossEncoder  # type: ignore[import-not-found]

        self.model = CrossEncoder(model_name)
        # label order for nli-deberta-v3 models: (contradiction, entailment, neutral)
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


def band_pairs(
    ctx: AnalysisContext, scorer: NliScorer
) -> tuple[list[UnitPair], list[tuple[UnitPair, float]]]:
    """Score unclaimed candidate pairs.

    Returns (cleared, not_cleared) where not_cleared is ranked by score
    descending (the jury adjudicates it in that order).
    """
    unclaimed = [p for p in ctx.pairs if not ctx.is_claimed(p)]
    if not unclaimed:
        return [], []
    scores = scorer.contradiction_scores([(p.a.normalized, p.b.normalized) for p in unclaimed])
    cleared: list[UnitPair] = []
    not_cleared: list[tuple[UnitPair, float]] = []
    for p, s in zip(unclaimed, scores, strict=True):
        if s < TAU_CLEAR:
            cleared.append(p)
        else:
            not_cleared.append((p, s))
    not_cleared.sort(key=lambda t: -t[1])
    return cleared, not_cleared


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

    cleared, not_cleared = band_pairs(ctx, scorer)
    ctx.lanes_ran.add("nli")
    ctx.corpus.notes.append(
        f"NLI lane: {len(cleared)} pair(s) auto-cleared as compatible, "
        f"{len(not_cleared)} left for adjudication"
        + ("" if cfg.lane_jury else " (enable the jury lane to adjudicate them)")
    )

    if cfg.lane_jury:
        # the jury consumes only the not-cleared band, best-scored first
        ctx.nli_not_cleared = not_cleared
    # standalone: no finding is ever emitted from an NLI score alone — a high
    # contradiction score cannot distinguish "conflicting" from merely
    # "different" prescriptions (measured; see module docstring)
    return findings
