"""Regression tests for the adversarial-review findings on the triage
baseline: merge-order verdict theft, ambiguous adoption, degraded-run
missing stamps, corrupt-file safety, config/CLI path semantics, and the
budget gate."""

from __future__ import annotations

import json
from pathlib import Path

from detangle.baseline import (
    Baseline,
    apply_baseline,
    finding_pair_key,
    load_baseline,
    prune_baseline,
)
from detangle.cli import main
from detangle.config import Config, load_config
from detangle.findings import Evidence, Finding
from detangle.ir import (
    Activation,
    ActivationMode,
    ConfigFile,
    Ecosystem,
    InstructionUnit,
    Layer,
    SourceSpan,
)
from detangle.pipeline import scan
from detangle.taxonomy import Severity

from .conftest import write_tree

D = "2026-08-31"


def _unit(text: str, path: str = "CLAUDE.md", line: int = 1) -> InstructionUnit:
    cf = ConfigFile(
        path=path,
        ecosystem=Ecosystem.CLAUDE_CODE,
        layer=Layer.PROJECT,
        tier=20,
        activation=Activation(mode=ActivationMode.ALWAYS),
        text="",
        mechanism="memory",
    )
    return InstructionUnit(
        text=text,
        normalized=text.lower(),
        span=SourceSpan(path, line, line),
        file=cf,
        activation=cf.activation,
    )


def _pair_finding(code: str, u1: InstructionUnit, u2: InstructionUnit, msg: str) -> Finding:
    return Finding(
        code=code,
        message=msg,
        severity=Severity.WARNING,
        evidence=[Evidence(u1.span, u1.text), Evidence(u2.span, u2.text)],
        units=[u1, u2],
        lanes=("jury",),
    )


def _unitless(code: str, msg: str, quote: str, line: int) -> Finding:
    return Finding(
        code=code,
        message=msg,
        severity=Severity.WARNING,
        evidence=[Evidence(SourceSpan("CLAUDE.md", line, line), quote)],
    )


class TestMergeOrder:
    def test_exact_match_beats_sibling_adoption(self):
        """A sibling-family finding processed first must not steal the entry
        whose byte-exact fingerprint match comes later in the list."""
        u1, u2 = _unit("Always cite sources."), _unit("Never cite sources.", "AGENTS.md")
        f_exact = _pair_finding("DTC02", u1, u2, "conditional clash")
        b = Baseline()
        apply_baseline([f_exact], b, D)
        b.entries[f_exact.fingerprint].status = "accepted"
        b.entries[f_exact.fingerprint].note = "human said fine"

        f_sibling = _pair_finding("DTC01", u1, u2, "hard clash")  # same pair, sibling code
        out = apply_baseline([f_sibling, f_exact], b, D)  # sibling FIRST
        assert out.tags[f_exact.fingerprint] == "accepted", "human verdict must stay put"
        assert out.tags[f_sibling.fingerprint] == "new", "sibling is genuinely untriaged"
        assert b.entries[f_exact.fingerprint].note == "human said fine"

    def test_ambiguous_adoption_disambiguates_by_message(self):
        """Two unit-less findings sharing a pair_key must re-attach to THEIR
        entries after a pure line shift, not swap verdicts."""
        fa5 = _unitless("DTX01", "zero-width characters", "some line", 5)
        fb5 = _unitless("DTX01", "bidi override", "some line", 5)
        assert finding_pair_key(fa5) == finding_pair_key(fb5)
        b = Baseline()
        apply_baseline([fa5, fb5], b, D)
        b.entries[fa5.fingerprint].status = "accepted"
        b.entries[fb5.fingerprint].status = "open"

        fa9 = _unitless("DTX01", "zero-width characters", "some line", 9)
        fb9 = _unitless("DTX01", "bidi override", "some line", 9)
        out = apply_baseline([fb9, fa9], b, D)  # adversarial order
        assert out.tags[fa9.fingerprint] == "accepted"
        assert out.tags[fb9.fingerprint] == "known"

    def test_truly_ambiguous_adoption_refuses(self):
        """When no tie-break resolves the candidates, the finding surfaces as
        new rather than inheriting an arbitrary verdict."""
        fa5 = _unitless("DTX01", "zero-width characters", "some line", 5)
        fb5 = _unitless("DTX01", "bidi override", "some line", 5)
        b = Baseline()
        apply_baseline([fa5, fb5], b, D)
        b.entries[fa5.fingerprint].status = "accepted"
        b.entries[fb5.fingerprint].status = "accepted"

        # both messages changed too: two candidates, no code/message tie-break
        fc9 = _unitless("DTX01", "confusable homoglyphs", "some line", 9)
        out = apply_baseline([fc9], b, D)
        assert out.tags[fc9.fingerprint] == "new"

    def test_empty_identity_findings_never_cross_adopt(self):
        f1 = Finding(code="DTP04", message="budget exceeded", severity=Severity.WARNING)
        f2 = Finding(code="DTP04", message="listing truncated", severity=Severity.WARNING)
        assert finding_pair_key(f1) == "" == finding_pair_key(f2)
        b = Baseline()
        apply_baseline([f1], b, D)
        b.entries[f1.fingerprint].status = "accepted"
        out = apply_baseline([f2], b, D)
        assert out.tags[f2.fingerprint] == "new", "no content identity -> no adoption"


class TestDegradedRuns:
    def test_lane_not_run_does_not_stamp_missing(self):
        u1, u2 = _unit("Do X."), _unit("Do not do X.", "AGENTS.md")
        f_jury = _pair_finding("DTC01", u1, u2, "jury found this")
        b = Baseline()
        apply_baseline([f_jury], b, D)
        assert b.entries[f_jury.fingerprint].lanes == ["jury"]

        out = apply_baseline([], b, D, ran_lanes={"deterministic"})
        assert out.counts["unchecked"] == 1 and out.counts["missing"] == 0
        assert b.entries[f_jury.fingerprint].missing_since is None
        assert prune_baseline(b) == 0, "prune must not eat what this run couldn't see"

    def test_disabled_rule_does_not_stamp_missing(self):
        u1, u2 = _unit("Do X."), _unit("Do not do X.", "AGENTS.md")
        f = _pair_finding("DTC01", u1, u2, "clash")
        f.lanes = ("deterministic",)
        b = Baseline()
        apply_baseline([f], b, D)
        out = apply_baseline(
            [], b, D, ran_lanes={"deterministic"}, disabled_codes=frozenset({"DTC01"})
        )
        assert out.counts["unchecked"] == 1 and out.counts["missing"] == 0

    def test_full_run_still_stamps_missing(self):
        u1, u2 = _unit("Do X."), _unit("Do not do X.", "AGENTS.md")
        f_jury = _pair_finding("DTC01", u1, u2, "jury found this")
        b = Baseline()
        apply_baseline([f_jury], b, D)
        out = apply_baseline([], b, D, ran_lanes={"deterministic", "nli", "jury", "screen"})
        assert out.counts["missing"] == 1
        assert b.entries[f_jury.fingerprint].missing_since == D


class TestMessageChurn:
    def test_llm_finding_resight_keeps_stored_wording(self):
        u1, u2 = _unit("Do X."), _unit("Do not do X.", "AGENTS.md")
        f1 = _pair_finding("DTC01", u1, u2, "Jury verdict CONTRADICTORY: phrasing one")
        b = Baseline()
        apply_baseline([f1], b, D)
        f2 = _pair_finding("DTC01", u1, u2, "Jury verdict CONTRADICTORY: phrasing two")
        assert f1.fingerprint == f2.fingerprint
        apply_baseline([f2], b, D)
        assert "phrasing one" in b.entries[f1.fingerprint].message, (
            "re-worded LLM verdicts must not churn the artifact"
        )


class TestCorruptBaseline:
    def test_scan_refuses_to_overwrite_corrupt_file(self, tmp_path: Path):
        write_tree(tmp_path, {"CLAUDE.md": "# R\n\nRetry flaky tests at most 3 times.\n"})
        bpath = tmp_path / ".detangle-baseline.json"
        bpath.write_text("{ not json !!!")
        cfg = Config(root=tmp_path)
        cfg.baseline_path = bpath
        cfg.update_baseline = True
        result = scan(cfg)
        assert bpath.read_text() == "{ not json !!!", "corrupt file must survive"
        assert any("refusing to overwrite" in w or "not updated" in w for w in result.warnings)

    def test_baseline_set_refuses_corrupt_file(self, tmp_path: Path, capsys):
        (tmp_path / ".detangle-baseline.json").write_text("[broken")
        assert main(["baseline", "set", "DTC01:abc", "open", str(tmp_path)]) == 2
        assert "refusing to modify" in capsys.readouterr().err

    def test_load_never_raises_on_binary_or_deep_json(self, tmp_path: Path):
        p = tmp_path / "b.json"
        p.write_bytes(b"\xff\xfe\x00garbage")
        assert load_baseline(p).corrupt
        p.write_text("[" * 200000 + "]" * 200000)
        b = load_baseline(p)  # must not raise RecursionError
        assert b.corrupt or not b.entries


class TestConfigAndCli:
    def test_baseline_table_without_path_enables_stage(self, tmp_path: Path):
        (tmp_path / ".detangle.toml").write_text("[detangle.baseline]\nupdate = true\n")
        cfg = load_config(tmp_path)
        assert cfg.update_baseline is True
        assert cfg.baseline_path == Path(".detangle-baseline.json")

    def test_bare_baseline_flag_keeps_configured_path(self, tmp_path: Path):
        write_tree(tmp_path, {"CLAUDE.md": "# R\n\nRetry flaky tests at most 3 times.\n"})
        (tmp_path / ".detangle.toml").write_text('[detangle.baseline]\npath = "custom-bl.json"\n')
        code = main(
            [
                "scan",
                str(tmp_path),
                "--baseline",
                "--update-baseline",
                "--format",
                "json",
                "--output",
                str(tmp_path / "r.json"),
            ]
        )
        assert code == 0
        assert (tmp_path / "custom-bl.json").exists(), "bare --baseline must not clobber TOML path"
        assert not (tmp_path / ".detangle-baseline.json").exists()

    def test_baseline_flag_pointing_at_directory_errors(self, tmp_path: Path, capsys):
        write_tree(tmp_path, {"CLAUDE.md": "# R\n\nhello\n"})
        try:
            main(["scan", "--baseline", str(tmp_path)])
        except SystemExit as e:
            assert e.code == 2
        else:  # pragma: no cover
            raise AssertionError("expected SystemExit(2)")
        assert "expects a file" in capsys.readouterr().err

    def test_unwritable_baseline_warns_instead_of_crashing(self, tmp_path: Path):
        # parent-is-a-file: load already fails (ENOTDIR) -> corrupt-refusal path
        write_tree(tmp_path, {"CLAUDE.md": "# R\n\nRetry flaky tests at most 3 times.\n"})
        blocker = tmp_path / "blocker"
        blocker.write_text("a file, not a dir")
        cfg = Config(root=tmp_path)
        cfg.baseline_path = blocker / "baseline.json"
        cfg.update_baseline = True
        result = scan(cfg)  # must not raise
        assert any("not updated" in w or "unreadable" in w for w in result.warnings)

    def test_write_oserror_warns_instead_of_crashing(self, tmp_path: Path, monkeypatch):
        # load succeeds (no file) but the write itself fails
        write_tree(tmp_path, {"CLAUDE.md": "# R\n\nRetry flaky tests at most 3 times.\n"})

        def boom(b, path):
            raise OSError("disk full")

        monkeypatch.setattr("detangle.baseline.save_baseline", boom)
        cfg = Config(root=tmp_path)
        cfg.baseline_path = tmp_path / ".detangle-baseline.json"
        cfg.update_baseline = True
        result = scan(cfg)  # must not raise
        assert any("could not write" in w for w in result.warnings)


class TestBudgetGate:
    def test_budget_counts_reality_not_the_view(self, tmp_path: Path):
        write_tree(
            tmp_path,
            {
                "CLAUDE.md": "# R\n\nRetry flaky tests at most 3 times.\n",
                "AGENTS.md": "# R\n\nRetry flaky tests exactly 5 times.\n",
            },
        )
        bpath = tmp_path / ".detangle-baseline.json"
        cfg = Config(root=tmp_path)
        cfg.baseline_path = bpath
        cfg.update_baseline = True
        r1 = scan(cfg)
        n = len(r1.findings)
        assert n >= 2
        data = json.loads(bpath.read_text())
        for e in data["entries"]:
            e["status"] = "open"
        bpath.write_text(json.dumps(data))

        cfg2 = Config(root=tmp_path)
        cfg2.baseline_path = bpath
        cfg2.only_new = True
        cfg2.conflict_budget = n - 1  # over budget in reality
        r2 = scan(cfg2)
        assert not r2.findings, "only-new view is empty"
        assert r2.exit_code() == 1, "the budget must gate reality, not the filtered view"


class TestMarkdownWarnings:
    def test_markdown_report_carries_notes(self, tmp_path: Path):
        from detangle.report import render_markdown

        write_tree(tmp_path, {"CLAUDE.md": "# R\n\nhello\n"})
        cfg = Config(root=tmp_path)
        cfg.baseline_path = tmp_path / ".detangle-baseline.json"
        (tmp_path / ".detangle-baseline.json").write_text("{ corrupt")
        result = scan(cfg)
        md = render_markdown(result)
        assert "## Notes" in md and "refusing to overwrite" in md
