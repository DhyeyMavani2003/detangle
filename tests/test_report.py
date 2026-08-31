"""Reporter tests: render_json / render_sarif / render_markdown / render_console
on a real ScanResult produced by pipeline.scan over a seeded config tree."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from detangle import __version__
from detangle.config import Config
from detangle.pipeline import ScanResult, scan
from detangle.report import render_console, render_json, render_markdown, render_sarif
from detangle.taxonomy import RULES


def write(root: Path, relpath: str, text: str) -> None:
    p = root / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def seed_tree(root: Path) -> None:
    """A tree that exercises several rule classes at once."""
    write(
        root,
        "CLAUDE.md",
        "# Project guide\n"
        "\n"
        "## Git\n"
        "- Never push directly to main.\n"
        "- Retry flaky tests at most 3 times.\n"
        "\n"
        "## Style\n"
        "- Be concise in your replies.\n",
    )
    write(
        root,
        "AGENTS.md",
        "# Agent instructions\n"
        "\n"
        "- Feel free to push directly to main for hotfixes.\n"
        "- Retry flaky tests exactly 5 times.\n"
        "- Always explain your reasoning in detail.\n"
        "- See docs/architecture.md for the system overview.\n",
    )
    write(
        root,
        ".claude/skills/deploy-helper/SKILL.md",
        "---\n"
        "name: deploy-helper\n"
        "description: Use when the user wants to deploy, release, or ship the application "
        "to production environments.\n"
        "---\n"
        "Deploy by running the release pipeline.\n",
    )
    write(
        root,
        ".claude/skills/release-helper/SKILL.md",
        "---\n"
        "name: release-helper\n"
        "description: Use when the user wants to release, ship, or deploy the application "
        "to production.\n"
        "---\n"
        "Release by tagging and running the pipeline.\n",
    )


@pytest.fixture(scope="module")
def result(tmp_path_factory: pytest.TempPathFactory) -> ScanResult:
    root = tmp_path_factory.mktemp("seeded")
    seed_tree(root)
    return scan(Config(root=root))


@pytest.fixture(scope="module")
def empty_result(tmp_path_factory: pytest.TempPathFactory) -> ScanResult:
    root = tmp_path_factory.mktemp("empty")
    write(root, "CLAUDE.md", "# Guide\n\n- Always run the tests before committing.\n")
    return scan(Config(root=root))


def test_seed_tree_produces_varied_findings(result: ScanResult) -> None:
    codes = {f.code for f in result.findings}
    assert "DTC03" in codes  # quantitative conflict
    assert "DTS01" in codes  # trigger overlap
    assert "DTR05" in codes  # stale reference
    assert len(codes) >= 3
    assert result.exit_code() == 1


# ---------------------------------------------------------------------------
# render_json
# ---------------------------------------------------------------------------


class TestRenderJson:
    def test_round_trips_through_json_loads(self, result: ScanResult) -> None:
        doc = json.loads(render_json(result))
        assert doc["tool"] == "detangle"
        assert doc["version"] == __version__
        assert set(doc) == {"tool", "version", "stats", "findings", "suppressed", "warnings"}

    def test_stats_keys(self, result: ScanResult) -> None:
        doc = json.loads(render_json(result))
        stats = doc["stats"]
        for key in ("files", "units", "pairs", "discover_s", "extract_s", "block_s", "total_s"):
            assert key in stats
        assert stats["files"] == 4

    def test_finding_keys_and_values(self, result: ScanResult) -> None:
        doc = json.loads(render_json(result))
        assert len(doc["findings"]) == len(result.findings)
        expected = {
            "code",
            "name",
            "severity",
            "message",
            "fingerprint",
            "evidence",
            "co_activation",
            "precedence",
            "witness",
            "suggestion",
            "confidence",
            "lanes",
            "tags",
        }
        for f in doc["findings"]:
            assert set(f) == expected
            assert f["code"] in RULES
            assert f["severity"] in ("error", "warning", "advisory", "info")
            assert f["fingerprint"].startswith(f["code"] + ":")
            assert f["lanes"] == ["deterministic"]
            assert 0.0 <= f["confidence"] <= 1.0
            for ev in f["evidence"]:
                assert set(ev) == {"path", "start_line", "end_line", "quote", "note"}
                assert ev["start_line"] >= 1
                assert ev["end_line"] >= ev["start_line"]

    def test_warnings_and_suppressed_lists(self, result: ScanResult) -> None:
        doc = json.loads(render_json(result))
        assert isinstance(doc["warnings"], list)
        assert doc["suppressed"] == []

    def test_empty_result(self, empty_result: ScanResult) -> None:
        doc = json.loads(render_json(empty_result))
        assert doc["findings"] == []


# ---------------------------------------------------------------------------
# render_sarif
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def sarif(result: ScanResult) -> dict:
    return json.loads(render_sarif(result))


class TestRenderSarif:
    def test_envelope(self, sarif: dict) -> None:
        assert "sarif-schema-2.1.0" in sarif["$schema"]
        assert sarif["version"] == "2.1.0"
        assert len(sarif["runs"]) == 1

    def test_driver_rules_are_unique_and_described(self, sarif: dict) -> None:
        driver = sarif["runs"][0]["tool"]["driver"]
        assert driver["name"] == "detangle"
        assert driver["version"] == __version__
        ids = [r["id"] for r in driver["rules"]]
        assert len(ids) == len(set(ids))
        for r in driver["rules"]:
            assert r["id"] in RULES
            assert r["shortDescription"]["text"]
            assert r["defaultConfiguration"]["level"] in ("error", "warning", "note")

    def test_every_result_rule_id_is_declared(self, sarif: dict) -> None:
        run = sarif["runs"][0]
        declared = {r["id"] for r in run["tool"]["driver"]["rules"]}
        assert run["results"]
        for res in run["results"]:
            assert res["ruleId"] in declared
            assert res["level"] in ("error", "warning", "note")

    def test_locations_have_uri_and_positive_start_line(self, sarif: dict) -> None:
        for res in sarif["runs"][0]["results"]:
            assert res["locations"]
            for loc in res["locations"] + res.get("relatedLocations", []):
                phys = loc["physicalLocation"]
                assert phys["artifactLocation"]["uri"]
                region = phys["region"]
                assert region["startLine"] >= 1
                if "endLine" in region:
                    assert region["endLine"] >= region["startLine"]

    def test_partial_fingerprints_present(self, sarif: dict, result: ScanResult) -> None:
        fingerprints = {f.fingerprint for f in result.findings}
        for res in sarif["runs"][0]["results"]:
            fp = res["partialFingerprints"]["detangle/v1"]
            assert fp in fingerprints

    def test_empty_result_is_valid_sarif(self, empty_result: ScanResult) -> None:
        doc = json.loads(render_sarif(empty_result))
        assert doc["version"] == "2.1.0"
        assert doc["runs"][0]["results"] == []
        assert doc["runs"][0]["tool"]["driver"]["rules"] == []


# ---------------------------------------------------------------------------
# render_markdown
# ---------------------------------------------------------------------------


class TestRenderMarkdown:
    def test_header_and_summary(self, result: ScanResult) -> None:
        md = render_markdown(result)
        assert md.startswith("# detangle report")
        assert f"**{len(result.findings)}** finding(s)" in md

    def test_code_headers_present(self, result: ScanResult) -> None:
        md = render_markdown(result)
        for f in result.findings:
            assert f"## {f.code} {f.name} — {f.severity.label}" in md

    def test_evidence_quotes_present(self, result: ScanResult) -> None:
        md = render_markdown(result)
        assert "> Retry flaky tests at most 3 times." in md
        assert "> Retry flaky tests exactly 5 times." in md
        dtc03 = next(f for f in result.findings if f.code == "DTC03")
        for ev in dtc03.evidence:
            assert f"`{ev.span}`" in md

    def test_fingerprints_present(self, result: ScanResult) -> None:
        md = render_markdown(result)
        for f in result.findings:
            assert f.fingerprint in md

    def test_empty_result(self, empty_result: ScanResult) -> None:
        md = render_markdown(empty_result)
        assert "No findings" in md


# ---------------------------------------------------------------------------
# render_console
# ---------------------------------------------------------------------------


class TestRenderConsole:
    def test_renders_findings_without_crashing(self, result: ScanResult, capsys) -> None:
        render_console(result)
        out = capsys.readouterr().out
        assert "detangle v" in out
        assert "DTC03" in out
        assert "finding(s)" in out

    def test_verbose_prints_notes(self, result: ScanResult, capsys) -> None:
        assert result.warnings  # the seed tree provokes at least one corpus note
        render_console(result, verbose=True)
        out = capsys.readouterr().out
        assert "notes:" in out

    def test_empty_result(self, empty_result: ScanResult, capsys) -> None:
        render_console(empty_result)
        assert "No findings" in capsys.readouterr().out
