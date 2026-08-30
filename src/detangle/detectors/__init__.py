"""Detector registry. Order matters: earlier detectors claim pairs first,
so one root cause yields one finding (numeric clash beats duplicate, etc.)."""

from __future__ import annotations

from .base import AnalysisContext, Detector, enabled_findings
from .conflicts import ConflictRouter, FormatConflictDetector
from .hygiene import (
    HiddenInstructionDetector,
    LintLeakageDetector,
    StaleReferenceDetector,
    UnreachableDetector,
)
from .redundancy import DuplicateDetector, TerminologyDetector
from .routing import (
    DescriptionMismatchDetector,
    DivergentInterpretationDetector,
    ShadowedNameDetector,
    TriggerOverlapDetector,
)

ALL_DETECTORS: tuple[type[Detector], ...] = (
    # pairwise, in claim-priority order
    ConflictRouter,
    FormatConflictDetector,
    DuplicateDetector,
    # corpus-level
    TerminologyDetector,
    HiddenInstructionDetector,
    StaleReferenceDetector,
    UnreachableDetector,
    LintLeakageDetector,
    TriggerOverlapDetector,
    ShadowedNameDetector,
    DescriptionMismatchDetector,
    DivergentInterpretationDetector,
)

__all__ = [
    "ALL_DETECTORS",
    "AnalysisContext",
    "Detector",
    "enabled_findings",
]
