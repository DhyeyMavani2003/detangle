"""Unit tests for suppression pragmas (detangle.suppress): parsing and covers()."""

from __future__ import annotations

from pathlib import Path

from detangle.findings import Evidence, Finding
from detangle.ingest.base import Corpus
from detangle.ir import Activation, ConfigFile, Ecosystem, Layer, SourceSpan
from detangle.suppress import Suppression, apply_suppressions, collect_suppressions
from detangle.taxonomy import Severity


def make_corpus(*files: tuple[str, str]) -> Corpus:
    """Corpus of (path, text) config files, no filesystem needed."""
    corpus = Corpus(root=Path("."))
    for path, text in files:
        corpus.add(
            ConfigFile(
                path=path,
                ecosystem=Ecosystem.CLAUDE_CODE,
                layer=Layer.PROJECT,
                tier=20,
                activation=Activation(),
                text=text,
            )
        )
    return corpus


def make_finding(code: str = "DTC03", path: str = "CLAUDE.md", line: int = 4) -> Finding:
    return Finding(
        code=code,
        message="seeded",
        severity=Severity.ERROR,
        evidence=[Evidence(SourceSpan(path, line, line), "quoted instruction")],
    )


# ---------------------------------------------------------------------------
# Pragma parsing (collect_suppressions)
# ---------------------------------------------------------------------------


class TestPragmaParsing:
    def test_single_code_with_reason(self) -> None:
        corpus = make_corpus(
            ("CLAUDE.md", "# T\n\n<!-- detangle-ignore DTC01: keep both until Q3 -->\n- X.\n")
        )
        sups, warnings = collect_suppressions(corpus)
        assert warnings == []
        assert len(sups) == 1
        s = sups[0]
        assert s.code == "DTC01"
        assert s.path == "CLAUDE.md"
        assert s.line == 3  # the pragma's own line
        assert s.file_wide is False
        assert s.reason == "keep both until Q3"

    def test_multi_code_pragma_expands_to_one_suppression_per_code(self) -> None:
        corpus = make_corpus(
            ("CLAUDE.md", "<!-- detangle-ignore DTC01, DTC03: both intentional -->\n- X.\n")
        )
        sups, warnings = collect_suppressions(corpus)
        assert warnings == []
        assert [s.code for s in sups] == ["DTC01", "DTC03"]
        assert all(s.line == 1 and s.reason == "both intentional" for s in sups)
        assert all(s.file_wide is False for s in sups)

    def test_ignore_file_scope(self) -> None:
        corpus = make_corpus(
            ("CLAUDE.md", "<!-- detangle-ignore-file DTR05: examples reference planned files -->\n")
        )
        sups, warnings = collect_suppressions(corpus)
        assert warnings == []
        assert len(sups) == 1
        assert sups[0].file_wide is True
        assert sups[0].code == "DTR05"

    def test_missing_reason_is_warned_but_still_suppresses(self) -> None:
        corpus = make_corpus(("CLAUDE.md", "# T\n<!-- detangle-ignore DTC01 -->\n- X.\n"))
        sups, warnings = collect_suppressions(corpus)
        assert len(sups) == 1
        assert sups[0].reason == ""
        assert len(warnings) == 1
        assert "CLAUDE.md:2" in warnings[0]
        assert "DTC01" in warnings[0]
        assert "no justification" in warnings[0]

    def test_multi_code_missing_reason_warns_once_naming_all_codes(self) -> None:
        corpus = make_corpus(("CLAUDE.md", "<!-- detangle-ignore DTC01,DTX01 -->\n"))
        sups, warnings = collect_suppressions(corpus)
        assert [s.code for s in sups] == ["DTC01", "DTX01"]
        assert len(warnings) == 1
        assert "DTC01" in warnings[0] and "DTX01" in warnings[0]

    def test_lowercase_pragma_is_accepted_and_code_normalized(self) -> None:
        corpus = make_corpus(("CLAUDE.md", "<!-- detangle-ignore dtc03: measured -->\n"))
        sups, warnings = collect_suppressions(corpus)
        assert warnings == []
        assert len(sups) == 1
        assert sups[0].code == "DTC03"

    def test_malformed_pragmas_are_ignored_silently(self) -> None:
        malformed = [
            "<!-- detangle-ignore DTZ01: unknown class letter -->",
            "<!-- detangle-ignore DTC1: code too short -->",
            "<!-- detangle-ignore -->",
            "<!-- detangle-ignore DTC01 trailing junk without colon -->",
            "<!-- detangle-ignore-everything DTC01: bad scope -->",
            "<!-- table of contents -->",
        ]
        corpus = make_corpus(("CLAUDE.md", "\n".join(malformed) + "\n"))
        sups, warnings = collect_suppressions(corpus)
        assert sups == []
        assert warnings == []

    def test_pragmas_are_collected_per_file(self) -> None:
        corpus = make_corpus(
            ("CLAUDE.md", "<!-- detangle-ignore DTC01: a -->\n"),
            ("AGENTS.md", "<!-- detangle-ignore-file DTR05: b -->\n"),
        )
        sups, _ = collect_suppressions(corpus)
        assert {(s.path, s.code, s.file_wide) for s in sups} == {
            ("CLAUDE.md", "DTC01", False),
            ("AGENTS.md", "DTR05", True),
        }


# ---------------------------------------------------------------------------
# covers(): the line-window logic
# ---------------------------------------------------------------------------


def make_sup(
    code: str = "DTC03",
    path: str = "CLAUDE.md",
    line: int = 3,
    file_wide: bool = False,
) -> Suppression:
    return Suppression(path=path, code=code, line=line, file_wide=file_wide, reason="why")


class TestCovers:
    def test_covers_evidence_on_the_pragma_line_and_window_below(self) -> None:
        sup = make_sup(line=3)
        assert sup.covers(make_finding(line=3)) is True  # pragma line itself
        assert sup.covers(make_finding(line=4)) is True  # instruction right below
        assert sup.covers(make_finding(line=9)) is True  # line + 6: window edge

    def test_does_not_cover_beyond_the_window_or_above(self) -> None:
        sup = make_sup(line=3)
        assert sup.covers(make_finding(line=10)) is False  # line + 7
        assert sup.covers(make_finding(line=2)) is False  # above the pragma

    def test_does_not_cover_other_paths_or_codes(self) -> None:
        sup = make_sup(line=3)
        assert sup.covers(make_finding(path="AGENTS.md", line=4)) is False
        assert sup.covers(make_finding(code="DTC01", line=4)) is False

    def test_code_match_is_case_insensitive_on_the_finding_side(self) -> None:
        assert make_sup(line=3).covers(make_finding(code="dtc03", line=4)) is True

    def test_file_wide_covers_any_line_in_the_same_file_only(self) -> None:
        sup = make_sup(line=1, file_wide=True)
        assert sup.covers(make_finding(line=999)) is True
        assert sup.covers(make_finding(path="AGENTS.md", line=1)) is False

    def test_any_matching_evidence_span_suffices(self) -> None:
        # pairwise finding: one evidence span in another file, one in-window
        f = Finding(
            code="DTC03",
            message="pair",
            severity=Severity.ERROR,
            evidence=[
                Evidence(SourceSpan("AGENTS.md", 50, 50), "other side"),
                Evidence(SourceSpan("CLAUDE.md", 5, 5), "suppressed side"),
            ],
        )
        assert make_sup(line=3).covers(f) is True


# ---------------------------------------------------------------------------
# apply_suppressions
# ---------------------------------------------------------------------------


class TestApplySuppressions:
    def test_splits_findings_into_kept_and_suppressed(self) -> None:
        covered = make_finding(line=4)
        out_of_window = make_finding(line=40)
        other_code = make_finding(code="DTC01", line=4)
        sup = make_sup(line=3)
        kept, suppressed = apply_suppressions([covered, out_of_window, other_code], [sup])
        assert kept == [out_of_window, other_code]
        assert suppressed == [(covered, sup)]

    def test_no_suppressions_keeps_everything(self) -> None:
        findings = [make_finding(), make_finding(code="DTC01")]
        kept, suppressed = apply_suppressions(findings, [])
        assert kept == findings
        assert suppressed == []

    def test_end_to_end_collect_then_apply(self) -> None:
        corpus = make_corpus(
            ("CLAUDE.md", "# T\n\n<!-- detangle-ignore DTC03: seeded -->\n- Retry 3 times.\n")
        )
        sups, _ = collect_suppressions(corpus)
        kept, suppressed = apply_suppressions([make_finding(line=4)], sups)
        assert kept == []
        assert len(suppressed) == 1
        assert suppressed[0][1].reason == "seeded"
