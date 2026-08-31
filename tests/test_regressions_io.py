"""Regression tests for verified I/O, CLI, config, cache, and report bugs.

Each test class reproduces one reviewed finding's original scenario:

- #24  scan hangs forever on FIFO/special files named like config files
- #25  diff mode drops all findings when the scan root is a repo subdirectory
- #26  diff mode drops findings for non-ASCII paths (git core.quotepath)
- #28  UTF-8 BOM defeats frontmatter detection
- #29  non-numeric config values crash with a raw ValueError traceback
- #30  bad --config / unwritable --output crash with OSError tracebacks
- #31  VerdictCache crashes on valid-JSON non-dict cache files
- #32  SARIF artifactLocation.uri is not a valid URI for space/non-ASCII paths
- #33  scan -v crashes on rich markup in suppression reasons/warnings
- #36  root-anchored .gitignore patterns treated as match-anywhere
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from detangle.cache import VerdictCache
from detangle.cli import main
from detangle.config import Config, ConfigError, load_config
from detangle.ingest import discover
from detangle.ingest.base import read_text, walk_repo
from detangle.ir import ActivationMode
from detangle.pipeline import scan
from detangle.report import render_console, render_sarif


def write(root: Path, relpath: str, text: str) -> Path:
    p = root / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=test@example.com", "-c", "user.name=Test", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


CONFLICT_BODY = (
    "# Guide\n\n- Retry flaky tests at most 3 times.\n- Retry flaky tests exactly 5 times.\n"
)


# ---------------------------------------------------------------------------
# 24: FIFO/special files must not hang the scan
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="mkfifo not available")
class TestFifoDoesNotHang:
    def test_scan_completes_with_fifo_named_agents_md(self, tmp_path: Path) -> None:
        write(tmp_path, "CLAUDE.md", "# Guide\n\n- Always run the tests.\n")
        os.mkfifo(tmp_path / "AGENTS.md")
        # Run in a subprocess so a regression fails with TimeoutExpired
        # instead of hanging the suite.
        proc = subprocess.run(
            [sys.executable, "-m", "detangle", "scan", str(tmp_path), "--format", "json"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        doc = json.loads(proc.stdout)
        assert doc["tool"] == "detangle"

    def test_read_text_returns_none_for_fifo(self, tmp_path: Path) -> None:
        fifo = tmp_path / "pipe.md"
        os.mkfifo(fifo)
        result: list[str | None] = []
        t = threading.Thread(target=lambda: result.append(read_text(fifo)), daemon=True)
        t.start()
        t.join(timeout=30)
        assert not t.is_alive(), "read_text blocked on a FIFO"
        assert result == [None]


# ---------------------------------------------------------------------------
# 25: diff mode with scan root below the git repo root
# ---------------------------------------------------------------------------


class TestDiffInRepoSubdirectory:
    def test_repo_relative_paths_are_translated_to_scan_root(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        app = tmp_path / "app"
        write(tmp_path, "app/CLAUDE.md", "# Guide\n\n- Retry flaky tests at most 3 times.\n")
        git(tmp_path, "init", "-b", "main")
        git(tmp_path, "add", "-A")
        git(tmp_path, "commit", "-m", "clean state")
        write(tmp_path, "app/AGENTS.md", "# Agents\n\n- Retry flaky tests exactly 5 times.\n")
        git(tmp_path, "add", "-A")
        git(tmp_path, "commit", "-m", "add conflicting AGENTS.md")

        code = main(["diff", str(app), "--base", "HEAD~1", "--format", "json"])
        captured = capsys.readouterr()
        doc = json.loads(captured.out)
        assert code == 1
        assert "could not compute git diff" not in captured.err
        codes = [f["code"] for f in doc["findings"]]
        assert "DTC03" in codes
        dtc03 = next(f for f in doc["findings"] if f["code"] == "DTC03")
        assert any(ev["path"] == "AGENTS.md" for ev in dtc03["evidence"])

    def test_changes_outside_scan_root_do_not_leak_in(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        app = tmp_path / "app"
        write(tmp_path, "app/CLAUDE.md", CONFLICT_BODY)
        write(tmp_path, "other/notes.md", "unrelated\n")
        git(tmp_path, "init", "-b", "main")
        git(tmp_path, "add", "-A")
        git(tmp_path, "commit", "-m", "state with conflict")
        write(tmp_path, "other/notes.md", "changed elsewhere in the repo\n")
        git(tmp_path, "add", "-A")
        git(tmp_path, "commit", "-m", "touch only other/")

        code = main(["diff", str(app), "--base", "HEAD~1", "--format", "json"])
        doc = json.loads(capsys.readouterr().out)
        # The conflict pre-exists; only other/ changed, so diff reports nothing.
        assert code == 0
        assert doc["findings"] == []


# ---------------------------------------------------------------------------
# 26: non-ASCII paths in diff mode (git core.quotepath)
# ---------------------------------------------------------------------------


class TestDiffNonAsciiPaths:
    def test_unicode_path_findings_survive_diff_filter(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        write(tmp_path, "döcs/CLAUDE.md", "# Guide\n\n- Always run the tests.\n")
        git(tmp_path, "init", "-b", "main")
        git(tmp_path, "add", "-A")
        git(tmp_path, "commit", "-m", "clean state")
        write(tmp_path, "döcs/CLAUDE.md", CONFLICT_BODY)
        git(tmp_path, "add", "-A")
        git(tmp_path, "commit", "-m", "introduce conflict")

        code = main(["diff", str(tmp_path), "--base", "HEAD~1", "--format", "json"])
        doc = json.loads(capsys.readouterr().out)
        assert code == 1
        dtc03 = [f for f in doc["findings"] if f["code"] == "DTC03"]
        assert dtc03
        assert any(ev["path"] == "döcs/CLAUDE.md" for ev in dtc03[0]["evidence"])


# ---------------------------------------------------------------------------
# 28: UTF-8 BOM must not defeat frontmatter detection
# ---------------------------------------------------------------------------


class TestBomFrontmatter:
    BOM_RULE = b'\xef\xbb\xbf---\npaths: "src/**"\n---\nRule body here.\n'

    def test_read_text_strips_bom(self, tmp_path: Path) -> None:
        p = tmp_path / "bom.md"
        p.write_bytes(self.BOM_RULE)
        text = read_text(p)
        assert text is not None
        assert not text.startswith("﻿")
        assert text.startswith("---\n")

    def test_bom_rule_keeps_path_scope_and_clean_body(self, tmp_path: Path) -> None:
        (tmp_path / ".claude" / "rules").mkdir(parents=True)
        (tmp_path / ".claude" / "rules" / "bom.md").write_bytes(self.BOM_RULE)
        corpus = discover(Config(root=tmp_path))
        cf = {c.path: c for c in corpus.files}[".claude/rules/bom.md"]
        # Before the fix: mode=ALWAYS, globs=() and the frontmatter leaked
        # into the instruction text.
        assert cf.activation.mode == ActivationMode.PATH
        assert cf.activation.globs == ("src/**",)
        assert "paths:" not in cf.text


# ---------------------------------------------------------------------------
# 29: non-numeric config values raise ConfigError, not raw ValueError
# ---------------------------------------------------------------------------


class TestConfigNumericValidation:
    @pytest.mark.parametrize(
        ("toml", "key"),
        [
            ('conflict_budget = "lots"\n', "conflict_budget"),
            ('max_pairs = "1e6"\n', "max_pairs"),
            ('similarity_threshold = "high"\n', "similarity_threshold"),
            ('[jury]\nmax_pairs = "many"\n', "jury.max_pairs"),
        ],
    )
    def test_bad_numeric_value_is_config_error(self, tmp_path: Path, toml: str, key: str) -> None:
        (tmp_path / ".detangle.toml").write_text(toml, encoding="utf-8")
        with pytest.raises(ConfigError, match=key):
            load_config(tmp_path)

    def test_cli_exits_two_with_clean_message(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        write(tmp_path, "CLAUDE.md", "# Guide\n\n- Always run the tests.\n")
        (tmp_path / ".detangle.toml").write_text('conflict_budget = "lots"\n', encoding="utf-8")
        with pytest.raises(SystemExit) as exc:
            main(["scan", str(tmp_path)])
        assert exc.value.code == 2
        assert "error:" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 30: bad --config / --output paths exit 2 cleanly instead of raising OSError
# ---------------------------------------------------------------------------


class TestCliPathErrors:
    @pytest.fixture()
    def tree(self, tmp_path: Path) -> Path:
        write(tmp_path, "CLAUDE.md", "# Guide\n\n- Always run the tests.\n")
        return tmp_path

    def _expect_exit_two(self, argv: list[str], capsys: pytest.CaptureFixture[str]) -> str:
        with pytest.raises(SystemExit) as exc:
            main(argv)
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "error:" in err
        return err

    def test_config_missing_file(self, tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
        self._expect_exit_two(["scan", str(tree), "--config", str(tree / "nope.toml")], capsys)

    def test_config_is_a_directory(self, tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
        self._expect_exit_two(["scan", str(tree), "--config", str(tree)], capsys)

    def test_output_in_nonexistent_dir(
        self, tree: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        dest = tree / "no-such-dir" / "out.json"
        self._expect_exit_two(["scan", str(tree), "--format", "json", "-o", str(dest)], capsys)

    def test_output_is_a_directory(self, tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
        self._expect_exit_two(["scan", str(tree), "--format", "json", "-o", str(tree)], capsys)


# ---------------------------------------------------------------------------
# 31: VerdictCache tolerates valid-JSON non-dict cache files
# ---------------------------------------------------------------------------


class TestVerdictCacheNonDict:
    @pytest.mark.parametrize("payload", ['["corrupted"]', '"just a string"', "42", "null"])
    def test_non_dict_json_resets_to_empty(self, tmp_path: Path, payload: str) -> None:
        (tmp_path / "verdicts.json").write_text(payload, encoding="utf-8")
        cache = VerdictCache(tmp_path)
        assert cache.get("k") is None
        cache.put("k", {"verdict": "ok"})
        assert cache.get("k") == {"verdict": "ok"}
        cache.save()
        assert json.loads((tmp_path / "verdicts.json").read_text(encoding="utf-8")) == {
            "k": {"verdict": "ok"}
        }


# ---------------------------------------------------------------------------
# 32: SARIF artifactLocation.uri must be a valid URI
# ---------------------------------------------------------------------------


class TestSarifUriEncoding:
    def test_space_and_non_ascii_paths_are_percent_encoded(self, tmp_path: Path) -> None:
        write(tmp_path, "my döcs/CLAUDE.md", CONFLICT_BODY)
        result = scan(Config(root=tmp_path))
        assert result.findings
        doc = json.loads(render_sarif(result))
        uris = [
            loc["physicalLocation"]["artifactLocation"]["uri"]
            for res in doc["runs"][0]["results"]
            for loc in res["locations"] + res.get("relatedLocations", [])
        ]
        assert uris
        for uri in uris:
            assert " " not in uri
            assert uri.isascii()
        assert any(uri.startswith("my%20d%C3%B6cs/") for uri in uris)


# ---------------------------------------------------------------------------
# 33: console verbose output must not parse user text as rich markup
# ---------------------------------------------------------------------------


class TestConsoleMarkupSafety:
    def test_bracket_tags_in_suppression_reason_do_not_crash(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        write(
            tmp_path,
            "CLAUDE.md",
            "# Guide\n\n<!-- detangle-ignore DTC03: keep both [/until] Q3 decision -->\n"
            "- Retry flaky tests at most 3 times.\n"
            "- Retry flaky tests exactly 5 times.\n",
        )
        result = scan(Config(root=tmp_path))
        assert result.suppressed
        render_console(result, verbose=True)  # raised rich.errors.MarkupError before
        assert "[/until]" in capsys.readouterr().out

    def test_bracket_tags_in_warnings_do_not_crash(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        write(tmp_path, "CLAUDE.md", "# Guide\n\n- Always run the tests.\n")
        result = scan(Config(root=tmp_path))
        result.warnings.append("corpus note with a [/bold] stray tag")
        render_console(result, verbose=True)
        assert "[/bold]" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# 36: root-anchored .gitignore patterns must not match at any depth
# ---------------------------------------------------------------------------


class TestGitignoreRootAnchor:
    def test_anchored_pattern_only_skips_root_entry(self, tmp_path: Path) -> None:
        write(tmp_path, ".gitignore", "/AGENTS.md\n")
        write(tmp_path, "AGENTS.md", "root — ignored by git\n")
        write(tmp_path, "packages/foo/AGENTS.md", "nested — NOT ignored by git\n")
        files = walk_repo(tmp_path)
        assert "AGENTS.md" not in files
        assert "packages/foo/AGENTS.md" in files

    def test_anchored_dir_pattern_skips_its_subtree_only(self, tmp_path: Path) -> None:
        write(tmp_path, ".gitignore", "/output\n")
        write(tmp_path, "output/generated.md", "ignored\n")
        write(tmp_path, "packages/output/AGENTS.md", "kept\n")
        files = walk_repo(tmp_path)
        assert "output/generated.md" not in files
        assert "packages/output/AGENTS.md" in files

    def test_bare_pattern_still_matches_any_depth(self, tmp_path: Path) -> None:
        write(tmp_path, ".gitignore", "scratch.md\n")
        write(tmp_path, "scratch.md", "ignored\n")
        write(tmp_path, "deep/nested/scratch.md", "also ignored\n")
        write(tmp_path, "kept.md", "kept\n")
        files = walk_repo(tmp_path)
        assert "scratch.md" not in files
        assert "deep/nested/scratch.md" not in files
        assert "kept.md" in files

    def test_anchored_config_file_is_scanned_again(self, tmp_path: Path) -> None:
        """End-to-end: findings in a nested AGENTS.md git does not ignore."""
        write(tmp_path, ".gitignore", "/AGENTS.md\n")
        write(tmp_path, "packages/foo/AGENTS.md", CONFLICT_BODY)
        result = scan(Config(root=tmp_path))
        assert any(
            f.code == "DTC03" and any(ev.span.path == "packages/foo/AGENTS.md" for ev in f.evidence)
            for f in result.findings
        )
