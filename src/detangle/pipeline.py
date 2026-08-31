"""The analysis pipeline: ingest → extract → co-activate → block → detect →
(optional lanes) → suppress → report-ready results."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .candidates import generate_pairs
from .config import Config
from .detectors import ALL_DETECTORS, AnalysisContext, enabled_findings
from .findings import Finding, dedupe
from .ingest import discover, extract_all_units
from .ingest.base import Corpus
from .ir import InstructionUnit, UnitPair
from .suppress import Suppression, apply_suppressions, collect_suppressions
from .taxonomy import Severity


@dataclass
class ScanResult:
    cfg: Config
    corpus: Corpus
    units: list[InstructionUnit]
    pairs: list[UnitPair]
    findings: list[Finding]
    suppressed: list[tuple[Finding, Suppression]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, float] = field(default_factory=dict)
    # triage-baseline annotations: fingerprint -> "new"|"known"|"regression",
    # plus the merge counters (new/known/regression/accepted_suppressed/missing)
    baseline_tags: dict[str, str] = field(default_factory=dict)
    baseline_stats: dict[str, int] = field(default_factory=dict)

    @property
    def worst_severity(self) -> Severity | None:
        return max((f.severity for f in self.findings), default=None)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self.findings:
            out[f.code] = out.get(f.code, 0) + 1
        return out

    def exit_code(self) -> int:
        gated = self.findings
        if self.cfg.fail_on_new and self.cfg.baseline_path is not None:
            # CI gate for the triage loop: known-but-open findings don't
            # block builds; only what's genuinely new (or regressed) does
            gated = [
                f for f in gated if self.baseline_tags.get(f.fingerprint) in ("new", "regression")
            ]
        if any(f.severity >= self.cfg.fail_on for f in gated):
            return 1
        if self.cfg.conflict_budget is not None and len(self.findings) > self.cfg.conflict_budget:
            return 1
        return 0


def scan(cfg: Config) -> ScanResult:
    t0 = time.monotonic()
    if cfg.deep:
        # thoroughness-first profile: every available lane, per-class screen
        # sweeps, jury cap lifted — built for overnight CI, hours are fine
        cfg.lane_screen = True
        cfg.lane_nli = True  # skips gracefully when the extra isn't installed
        cfg.jury_max_pairs = max(cfg.jury_max_pairs, 1000)
    if cfg.lane_screen:
        # the screen only nominates; the jury adjudicates its nominations
        cfg.lane_jury = True
    corpus = discover(cfg)
    t_discover = time.monotonic()

    # the screen lane reasons over weak/hedged sentences too (high-recall
    # extraction); deterministic detectors still only use strict instructions
    units = extract_all_units(corpus, keep_descriptive=cfg.lane_screen)
    t_extract = time.monotonic()

    pairs = generate_pairs(units, cfg)
    t_block = time.monotonic()

    ctx = AnalysisContext(cfg=cfg, corpus=corpus, units=units, pairs=pairs)
    findings: list[Finding] = []
    for det_cls in ALL_DETECTORS:
        det = det_cls()
        if not any(cfg.rule_enabled(c) for c in det.codes):
            continue
        findings.extend(enabled_findings(ctx, det.run(ctx)))

    # optional lanes refine/extend deterministic findings; their output goes
    # through the same rule-enable/severity-override filter as detectors
    if cfg.lane_nli:
        from .lanes.nli import run_nli_lane

        findings = enabled_findings(ctx, run_nli_lane(cfg, ctx, findings))
    if cfg.lane_jury:
        from .lanes.jury import run_jury_lane

        findings = enabled_findings(ctx, run_jury_lane(cfg, ctx, findings))

    findings = dedupe(findings)
    if not cfg.include_soft:
        findings = [f for f in findings if f.severity > Severity.ADVISORY]

    sups, sup_warnings = collect_suppressions(corpus)
    findings, suppressed = apply_suppressions(findings, sups)

    findings.sort(
        key=lambda f: (
            -int(f.severity),
            f.primary_span.path if f.primary_span else "",
            f.primary_span.start_line if f.primary_span else 0,
            f.code,
        )
    )

    # triage baseline: pre-fill human verdicts from previous runs, suppress
    # what a human already dismissed, tag what's genuinely new
    baseline_tags: dict[str, str] = {}
    baseline_stats: dict[str, int] = {}
    baseline_warnings: list[str] = []
    if cfg.baseline_path is not None:
        from .baseline import apply_baseline, load_baseline, save_baseline, today

        bpath = cfg.baseline_path
        if not bpath.is_absolute():
            bpath = cfg.root / bpath
        bl = load_baseline(bpath)
        baseline_warnings = list(bl.warnings)
        outcome = apply_baseline(findings, bl, today())
        findings = outcome.findings
        baseline_tags = outcome.tags
        baseline_stats = outcome.counts
        if cfg.only_new:
            findings = [
                f for f in findings if baseline_tags.get(f.fingerprint) in ("new", "regression")
            ]
        if cfg.update_baseline:
            save_baseline(outcome.baseline, bpath)
    t_end = time.monotonic()

    return ScanResult(
        cfg=cfg,
        corpus=corpus,
        units=units,
        pairs=pairs,
        findings=findings,
        suppressed=suppressed,
        baseline_tags=baseline_tags,
        baseline_stats=baseline_stats,
        warnings=list(corpus.notes) + sup_warnings + baseline_warnings,
        stats={
            "files": len(corpus.files),
            "units": len(units),
            "pairs": len(pairs),
            "discover_s": round(t_discover - t0, 3),
            "extract_s": round(t_extract - t_discover, 3),
            "block_s": round(t_block - t_extract, 3),
            "total_s": round(t_end - t0, 3),
        },
    )
