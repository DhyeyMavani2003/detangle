"""Integration tests for the triage-baseline wiring: pipeline tagging,
only-new filtering, fail-on-new gating, deep expansion, and the baseline
CLI subcommands (the human's answer surface)."""

from __future__ import annotations

import json
from pathlib import Path

from detangle.cli import main
from detangle.config import Config
from detangle.pipeline import scan

from .conftest import write_tree

# A tree with one crisp deterministic conflict — a genuinely non-intersecting
# numeric clash (DTC03 at error severity) plus the Zed first-match advisory,
# so the baseline tests exercise entries at two severities.
CONFLICT_TREE = {
    "CLAUDE.md": "# Rules\n\nRetry flaky tests at most 3 times.\n",
    "AGENTS.md": "# Rules\n\nRetry flaky tests exactly 5 times.\n",
}


def _scan(root: Path, **overrides) -> object:
    cfg = Config(root=root)
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return scan(cfg)


class TestPipelineBaseline:
    def test_first_run_tags_new_and_writes_baseline(self, tmp_path: Path):
        write_tree(tmp_path, CONFLICT_TREE)
        result = _scan(
            tmp_path, baseline_path=Path(".detangle-baseline.json"), update_baseline=True
        )
        assert result.findings, "expected the numeric clash to fire"
        assert all(result.baseline_tags.get(f.fingerprint) == "new" for f in result.findings)
        assert result.baseline_stats["new"] == len(result.findings)
        data = json.loads((tmp_path / ".detangle-baseline.json").read_text())
        assert data["version"] == 1 and data["entries"]

    def test_second_run_prefills_and_is_byte_stable(self, tmp_path: Path):
        write_tree(tmp_path, CONFLICT_TREE)
        bpath = tmp_path / ".detangle-baseline.json"
        _scan(tmp_path, baseline_path=bpath, update_baseline=True)
        first = bpath.read_bytes()
        result = _scan(tmp_path, baseline_path=bpath, update_baseline=True)
        assert bpath.read_bytes() == first, "unchanged repo must not churn the baseline"
        # entries stay 'new' until a human answers; the report still tags them
        assert all(t == "new" for t in result.baseline_tags.values())

    def test_accepted_suppresses_and_open_marks_known(self, tmp_path: Path):
        write_tree(tmp_path, CONFLICT_TREE)
        bpath = tmp_path / ".detangle-baseline.json"
        r1 = _scan(tmp_path, baseline_path=bpath, update_baseline=True)
        n = len(r1.findings)
        data = json.loads(bpath.read_text())
        data["entries"][0]["status"] = "accepted"
        data["entries"][0]["note"] = "target vs ceiling, intentional"
        for e in data["entries"][1:]:
            e["status"] = "open"
        bpath.write_text(json.dumps(data))

        r2 = _scan(tmp_path, baseline_path=bpath)
        assert len(r2.findings) == n - 1, "accepted finding must be suppressed"
        assert r2.baseline_stats["accepted_suppressed"] == 1
        assert all(t == "known" for t in r2.baseline_tags.values() if t != "accepted")

    def test_only_new_focuses_on_the_delta(self, tmp_path: Path):
        write_tree(tmp_path, CONFLICT_TREE)
        bpath = tmp_path / ".detangle-baseline.json"
        _scan(tmp_path, baseline_path=bpath, update_baseline=True)
        data = json.loads(bpath.read_text())
        for e in data["entries"]:
            e["status"] = "open"
        bpath.write_text(json.dumps(data))

        # introduce a brand-new conflict
        write_tree(
            tmp_path,
            {
                ".claude/rules/style.md": (
                    "Always write commit subjects in the imperative mood.\n"
                    "Never write commit subjects in the imperative mood.\n"
                )
            },
        )
        r = _scan(tmp_path, baseline_path=bpath, only_new=True)
        assert r.findings, "the new contradiction must surface"
        assert all(r.baseline_tags[f.fingerprint] == "new" for f in r.findings)
        assert not any("Retry" in ev.quote for f in r.findings for ev in f.evidence), (
            "known findings must be hidden under --only-new"
        )

    def test_fail_on_new_gates_only_the_delta(self, tmp_path: Path):
        write_tree(tmp_path, CONFLICT_TREE)
        bpath = tmp_path / ".detangle-baseline.json"
        _scan(tmp_path, baseline_path=bpath, update_baseline=True)
        data = json.loads(bpath.read_text())
        for e in data["entries"]:
            e["status"] = "open"
        bpath.write_text(json.dumps(data))

        from detangle.taxonomy import Severity

        r = _scan(tmp_path, baseline_path=bpath, fail_on_new=True, fail_on=Severity.WARNING)
        assert r.findings and r.exit_code() == 0, "known-but-open findings must not fail CI"

        write_tree(
            tmp_path,
            {
                ".claude/rules/style.md": (
                    "Always write commit subjects in the imperative mood.\n"
                    "Never write commit subjects in the imperative mood.\n"
                )
            },
        )
        r2 = _scan(tmp_path, baseline_path=bpath, fail_on_new=True, fail_on=Severity.WARNING)
        assert r2.exit_code() == 1, "a new finding must fail the gate"

    def test_resolved_reappearing_is_a_regression(self, tmp_path: Path):
        write_tree(tmp_path, CONFLICT_TREE)
        bpath = tmp_path / ".detangle-baseline.json"
        _scan(tmp_path, baseline_path=bpath, update_baseline=True)
        data = json.loads(bpath.read_text())
        for e in data["entries"]:
            e["status"] = "resolved"
        bpath.write_text(json.dumps(data))
        r = _scan(tmp_path, baseline_path=bpath, update_baseline=True)
        assert all(t == "regression" for t in r.baseline_tags.values())
        data = json.loads(bpath.read_text())
        assert all(e["status"] == "new" for e in data["entries"])


class TestDeepExpansion:
    def test_deep_enables_all_lanes_and_lifts_cap(self, tmp_path: Path, monkeypatch):
        from detangle.lanes.backends import JuryError

        def no_backend(cfg, role="jury"):
            raise JuryError("none in tests")

        monkeypatch.setattr("detangle.lanes.backends.make_backend", no_backend)
        write_tree(tmp_path, {"CLAUDE.md": "# T\n\nNever push to main.\n"})
        cfg = Config(root=tmp_path)
        cfg.deep = True
        result = scan(cfg)  # lanes skip gracefully without backends/models
        assert cfg.lane_screen and cfg.lane_jury and cfg.lane_nli
        assert cfg.jury_max_pairs >= 1000
        assert result is not None


class TestBaselineCli:
    def _seed(self, tmp_path: Path) -> Path:
        write_tree(tmp_path, CONFLICT_TREE)
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
        assert code == 1, "the untriaged DTC03 error legitimately fails the run"
        return tmp_path / ".detangle-baseline.json"

    def test_scan_flag_writes_default_baseline(self, tmp_path: Path):
        bpath = self._seed(tmp_path)
        assert bpath.exists()
        report = json.loads((tmp_path / "r.json").read_text())
        assert report["baseline"]["new"] >= 1
        assert all(f["baseline"] == "new" for f in report["findings"])

    def test_list_set_prune_cycle(self, tmp_path: Path, capsys):
        bpath = self._seed(tmp_path)
        assert main(["baseline", "list", str(tmp_path), "--status", "new"]) == 0
        out = capsys.readouterr().out
        assert "[new" in out and "detangle baseline set" in out

        fp = json.loads(bpath.read_text())["entries"][0]["fingerprint"]
        assert (
            main(["baseline", "set", fp, "accepted", str(tmp_path), "--note", "intentional"]) == 0
        )
        data = json.loads(bpath.read_text())
        entry = next(e for e in data["entries"] if e["fingerprint"] == fp)
        assert entry["status"] == "accepted" and entry["note"] == "intentional"

        # prefix matching: a short unambiguous prefix works
        fp2 = next(e["fingerprint"] for e in data["entries"] if e["fingerprint"] != fp)
        assert main(["baseline", "set", fp2[:10], "open", str(tmp_path)]) == 0

        # remove the conflict from the tree -> entries go missing -> prune
        (tmp_path / "AGENTS.md").write_text("# Rules\n\nBe kind.\n")
        assert (
            main(
                [
                    "scan",
                    str(tmp_path),
                    "--baseline",
                    "--update-baseline",
                    "--format",
                    "json",
                    "--output",
                    str(tmp_path / "r2.json"),
                ]
            )
            == 0
        )
        assert main(["baseline", "prune", str(tmp_path)]) == 0
        out = capsys.readouterr().out
        assert "pruned" in out

    def test_set_unknown_and_ambiguous(self, tmp_path: Path, capsys):
        self._seed(tmp_path)
        assert main(["baseline", "set", "nope", "open", str(tmp_path)]) == 2
        assert main(["baseline", "set", "DT", "open", str(tmp_path)]) == 2
        err = capsys.readouterr().err
        assert "no baseline entry" in err or "ambiguous" in err

    def test_only_new_requires_baseline(self, tmp_path: Path, capsys):
        write_tree(tmp_path, CONFLICT_TREE)
        try:
            main(["scan", str(tmp_path), "--only-new"])
        except SystemExit as e:
            assert e.code == 2
        else:  # pragma: no cover
            raise AssertionError("expected SystemExit(2)")
