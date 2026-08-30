"""Detector framework: analysis context, protocol, registry."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import Config
from ..findings import Finding
from ..ingest.base import Corpus
from ..ir import InstructionUnit, UnitPair


@dataclass
class AnalysisContext:
    cfg: Config
    corpus: Corpus
    units: list[InstructionUnit]
    pairs: list[UnitPair]
    # pair keys already claimed by a higher-priority detector; later detectors
    # skip claimed pairs so one root cause yields one finding
    claimed: set[str] = field(default_factory=set)

    def claim(self, pair: UnitPair) -> None:
        self.claimed.add(pair.key)

    def is_claimed(self, pair: UnitPair) -> bool:
        return pair.key in self.claimed


class Detector:
    """A detector emits findings for one or more taxonomy codes."""

    codes: tuple[str, ...] = ()
    name = "detector"

    def run(self, ctx: AnalysisContext) -> list[Finding]:  # pragma: no cover
        raise NotImplementedError


def enabled_findings(ctx: AnalysisContext, findings: list[Finding]) -> list[Finding]:
    """Filter by rule enablement and apply user severity overrides.

    Detector-chosen severities are kept unless the user explicitly overrode
    the rule's severity in config.
    """
    out: list[Finding] = []
    for f in findings:
        if not ctx.cfg.rule_enabled(f.code):
            continue
        if f.code in ctx.cfg.severity_overrides:
            f.severity = ctx.cfg.severity_overrides[f.code]
        out.append(f)
    return out
