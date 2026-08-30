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

    @property
    def worst_severity(self) -> Severity | None:
        return max((f.severity for f in self.findings), default=None)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self.findings:
            out[f.code] = out.get(f.code, 0) + 1
        return out

    def exit_code(self) -> int:
        if any(f.severity >= self.cfg.fail_on for f in self.findings):
            return 1
        if self.cfg.conflict_budget is not None and len(self.findings) > self.cfg.conflict_budget:
            return 1
        return 0


def scan(cfg: Config) -> ScanResult:
    t0 = time.monotonic()
    corpus = discover(cfg)
    t_discover = time.monotonic()

    units = extract_all_units(corpus)
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

    # optional lanes refine/extend deterministic findings
    if cfg.lane_nli:
        from .lanes.nli import run_nli_lane

        findings = run_nli_lane(cfg, ctx, findings)
    if cfg.lane_jury:
        from .lanes.jury import run_jury_lane

        findings = run_jury_lane(cfg, ctx, findings)

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
    t_end = time.monotonic()

    return ScanResult(
        cfg=cfg,
        corpus=corpus,
        units=units,
        pairs=pairs,
        findings=findings,
        suppressed=suppressed,
        warnings=list(corpus.notes) + sup_warnings,
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
