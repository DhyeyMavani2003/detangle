"""CLI tests: invoke detangle.cli.main([...]) on small config trees in tmp_path."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from detangle import __version__
from detangle.cli import main
from detangle.taxonomy import RULES


def write(root: Path, relpath: str, text: str) -> Path:
    p = root / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def make_clean_tree(root: Path) -> None:
    write(root, "CLAUDE.md", "# Guide\n\n- Always run the tests before committing.\n")


def make_error_tree(root: Path) -> None:
    """DTC03 (error) between CLAUDE.md and AGENTS.md, plus DTP05 advisories."""
    write(root, "CLAUDE.md", "# Guide\n\n- Retry flaky tests at most 3 times.\n")
    write(root, "AGENTS.md", "# Agents\n\n- Retry flaky tests exactly 5 times.\n")


def make_warning_tree(root: Path) -> None:
    """DTR05 stale-reference (warning) only."""
    write(root, "CLAUDE.md", "# Guide\n\n- See docs/architecture.md for the system overview.\n")


def make_advisory_tree(root: Path) -> None:
    """DTC08 pragmatic-tension (advisory) only."""
    write(
        root,
        "CLAUDE.md",
        "# Guide\n\n- Be concise in your replies.\n- Always explain your reasoning in detail.\n",
    )


def scan_json(capsys: pytest.CaptureFixture[str], root: Path, *extra: str) -> tuple[int, dict]:
    code = main(["scan", str(root), "--format", "json", *extra])
    doc = json.loads(capsys.readouterr().out)
    return code, doc


# ---------------------------------------------------------------------------
# scan: output formats
# ---------------------------------------------------------------------------


class TestScanFormats:
    def test_default_console_output(self, tmp_path: Path, capsys) -> None:
        make_error_tree(tmp_path)
        code = main(["scan", str(tmp_path)])
        out = capsys.readouterr().out
        assert code == 1
        assert "detangle v" in out
        assert "config files" in out
        assert "DTC03" in out
        assert "quantitative-conflict" in out
        assert "finding(s)" in out

    def test_console_clean_tree(self, tmp_path: Path, capsys) -> None:
        make_clean_tree(tmp_path)
        code = main(["scan", str(tmp_path)])
        out = capsys.readouterr().out
        assert code == 0
        assert "No findings" in out

    def test_format_json(self, tmp_path: Path, capsys) -> None:
        make_error_tree(tmp_path)
        code, doc = scan_json(capsys, tmp_path)
        assert code == 1
        assert doc["tool"] == "detangle"
        assert doc["version"] == __version__
        assert set(doc) == {"tool", "version", "stats", "findings", "suppressed", "warnings"}
        assert doc["stats"]["files"] == 2
        codes = [f["code"] for f in doc["findings"]]
        assert "DTC03" in codes
        for f in doc["findings"]:
            assert {"code", "severity", "message", "fingerprint", "evidence"} <= set(f)

    def test_format_sarif(self, tmp_path: Path, capsys) -> None:
        make_error_tree(tmp_path)
        code = main(["scan", str(tmp_path), "--format", "sarif"])
        doc = json.loads(capsys.readouterr().out)
        assert code == 1
        assert doc["version"] == "2.1.0"
        run = doc["runs"][0]
        assert run["tool"]["driver"]["name"] == "detangle"
        assert any(res["ruleId"] == "DTC03" for res in run["results"])

    def test_format_markdown(self, tmp_path: Path, capsys) -> None:
        make_error_tree(tmp_path)
        code = main(["scan", str(tmp_path), "--format", "markdown"])
        out = capsys.readouterr().out
        assert code == 1
        assert out.startswith("# detangle report")
        assert "## DTC03 quantitative-conflict — error" in out

    def test_output_writes_file(self, tmp_path: Path, capsys) -> None:
        make_error_tree(tmp_path)
        dest = tmp_path / "report.json"
        code = main(["scan", str(tmp_path), "--format", "json", "--output", str(dest)])
        out = capsys.readouterr().out
        assert code == 1
        assert dest.is_file()
        assert f"wrote {dest}" in out
        doc = json.loads(dest.read_text(encoding="utf-8"))
        assert doc["tool"] == "detangle"

    def test_scan_defaults_to_cwd(self, tmp_path: Path, capsys, monkeypatch) -> None:
        make_error_tree(tmp_path)
        monkeypatch.chdir(tmp_path)
        code, doc = scan_json(capsys, Path("."))
        assert code == 1
        assert any(f["code"] == "DTC03" for f in doc["findings"])


# ---------------------------------------------------------------------------
# scan: exit codes
# ---------------------------------------------------------------------------


class TestExitCodes:
    def test_clean_tree_exits_zero(self, tmp_path: Path, capsys) -> None:
        make_clean_tree(tmp_path)
        code, doc = scan_json(capsys, tmp_path)
        assert code == 0
        assert doc["findings"] == []

    def test_error_finding_exits_one(self, tmp_path: Path, capsys) -> None:
        make_error_tree(tmp_path)
        code, doc = scan_json(capsys, tmp_path)
        assert code == 1
        assert any(f["severity"] == "error" for f in doc["findings"])

    def test_warnings_pass_by_default(self, tmp_path: Path, capsys) -> None:
        make_warning_tree(tmp_path)
        code, doc = scan_json(capsys, tmp_path)
        assert code == 0
        assert {f["severity"] for f in doc["findings"]} == {"warning"}

    def test_fail_on_warning_makes_warnings_fail(self, tmp_path: Path, capsys) -> None:
        make_warning_tree(tmp_path)
        code, _ = scan_json(capsys, tmp_path, "--fail-on", "warning")
        assert code == 1

    def test_advisory_only_tree_exits_zero(self, tmp_path: Path, capsys) -> None:
        make_advisory_tree(tmp_path)
        code, doc = scan_json(capsys, tmp_path)
        assert code == 0
        assert doc["findings"]
        assert {f["severity"] for f in doc["findings"]} == {"advisory"}

    def test_nonexistent_path_exits_two(self, tmp_path: Path, capsys) -> None:
        with pytest.raises(SystemExit) as exc:
            main(["scan", str(tmp_path / "no-such-dir")])
        assert exc.value.code == 2


# ---------------------------------------------------------------------------
# scan: --select and --no-soft
# ---------------------------------------------------------------------------


class TestSelectAndSoft:
    def test_select_runs_only_that_rule(self, tmp_path: Path, capsys) -> None:
        make_error_tree(tmp_path)
        code, doc = scan_json(capsys, tmp_path, "--select", "DTC03")
        assert code == 1
        assert doc["findings"]
        assert {f["code"] for f in doc["findings"]} == {"DTC03"}

    def test_select_bogus_code_exits_two(self, tmp_path: Path, capsys) -> None:
        make_error_tree(tmp_path)
        with pytest.raises(SystemExit) as exc:
            main(["scan", str(tmp_path), "--select", "DTC99"])
        assert exc.value.code == 2
        assert "unknown rule code" in capsys.readouterr().err

    def test_no_soft_hides_advisories(self, tmp_path: Path, capsys) -> None:
        make_error_tree(tmp_path)
        _, full = scan_json(capsys, tmp_path)
        assert any(f["severity"] == "advisory" for f in full["findings"])
        code, doc = scan_json(capsys, tmp_path, "--no-soft")
        assert code == 1
        assert all(f["severity"] not in ("advisory", "info") for f in doc["findings"])
        assert any(f["code"] == "DTC03" for f in doc["findings"])


# ---------------------------------------------------------------------------
# rules / explain / version
# ---------------------------------------------------------------------------


class TestRulesCommand:
    def test_lists_all_rule_codes(self, capsys) -> None:
        assert main(["rules"]) == 0
        out = capsys.readouterr().out
        for code, r in RULES.items():
            assert code in out
            assert r.name in out


class TestExplainCommand:
    def test_explain_known_code(self, capsys) -> None:
        assert main(["explain", "DTC01"]) == 0
        out = capsys.readouterr().out
        assert "DTC01" in out
        assert "direct-contradiction" in out
        assert "error" in out

    def test_explain_bogus_code_exits_two(self, capsys) -> None:
        assert main(["explain", "BOGUS"]) == 2
        assert "unknown rule code" in capsys.readouterr().err

    def test_explain_accepts_fingerprint_prefix(self, capsys) -> None:
        assert main(["explain", "DTC03:abc"]) == 0
        assert "quantitative-conflict" in capsys.readouterr().out

    def test_explain_lowercase_code(self, capsys) -> None:
        assert main(["explain", "dtc01"]) == 0
        assert "direct-contradiction" in capsys.readouterr().out


class TestVersion:
    def test_version_flag(self, capsys) -> None:
        with pytest.raises(SystemExit) as exc:
            main(["--version"])
        assert exc.value.code == 0
        assert f"detangle {__version__}" in capsys.readouterr().out


class TestNoCommand:
    def test_no_command_prints_help(self, capsys) -> None:
        assert main([]) == 0
        assert "usage: detangle" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------


def git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=test@example.com", "-c", "user.name=Test", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


class TestDiffCommand:
    def test_diff_keeps_only_findings_touching_changed_files(self, tmp_path: Path, capsys) -> None:
        # Clean state on main: a stale reference lives only in CLAUDE.md.
        write(
            tmp_path,
            "CLAUDE.md",
            "# Guide\n\n- Retry flaky tests at most 3 times.\n"
            "- See docs/architecture.md for the system overview.\n",
        )
        git(tmp_path, "init", "-b", "main")
        git(tmp_path, "add", "-A")
        git(tmp_path, "commit", "-m", "clean state")

        # Feature branch introduces a conflicting AGENTS.md.
        git(tmp_path, "checkout", "-b", "feature")
        write(tmp_path, "AGENTS.md", "# Agents\n\n- Retry flaky tests exactly 5 times.\n")
        git(tmp_path, "add", "-A")
        git(tmp_path, "commit", "-m", "add conflicting AGENTS.md")

        code = main(["diff", str(tmp_path), "--base", "main", "--format", "json"])
        doc = json.loads(capsys.readouterr().out)
        assert code == 1

        codes = [f["code"] for f in doc["findings"]]
        assert "DTC03" in codes  # the new cross-file conflict touches AGENTS.md
        assert "DTR05" not in codes  # pre-existing, CLAUDE.md-only: filtered out
        for f in doc["findings"]:
            assert any(ev["path"] == "AGENTS.md" for ev in f["evidence"])

        # A full scan of the same tree still reports the stale reference.
        _, full = scan_json(capsys, tmp_path)
        assert "DTR05" in [f["code"] for f in full["findings"]]

    def test_diff_without_git_repo_warns_and_reports_all(self, tmp_path: Path, capsys) -> None:
        make_warning_tree(tmp_path)
        code = main(["diff", str(tmp_path), "--base", "main", "--format", "json"])
        captured = capsys.readouterr()
        assert code == 0
        assert "could not compute git diff" in captured.err
        doc = json.loads(captured.out)
        assert "DTR05" in [f["code"] for f in doc["findings"]]
