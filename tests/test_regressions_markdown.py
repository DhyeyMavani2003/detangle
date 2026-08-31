"""Regression tests for review findings #4, #13, #14, #27, #34, #35.

Each class reproduces one confirmed bug's original scenario and pins the
fixed behavior (markdown parsing, suppression line offsets, loose .mdc
frontmatter, and @import code-skipping/relative-path handling).
"""

from __future__ import annotations

from pathlib import Path

from detangle.config import Config
from detangle.findings import Evidence, Finding
from detangle.ingest import discover
from detangle.ir import ActivationMode, ConfigFile, SourceSpan
from detangle.markdown import iter_sentences, parse_blocks, split_frontmatter
from detangle.suppress import collect_suppressions
from detangle.taxonomy import Severity


def write(root: Path, relpath: str, text: str) -> Path:
    p = root / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def by_path(corpus) -> dict[str, ConfigFile]:
    return {cf.path: cf for cf in corpus.files}


# ---------------------------------------------------------------------------
# Finding 13: fenced code inside a bullet was folded into prose
# ---------------------------------------------------------------------------


class TestBulletNestedFence:
    def test_fence_under_bullet_is_code_not_prose(self) -> None:
        doc = (
            "- Use this template:\n"
            "  ```bash\n"
            "  # Never run this in prod. Always ask first.\n"
            "  rm -rf build/\n"
            "  ```\n"
        )
        blocks = parse_blocks(doc)
        assert [b.kind for b in blocks] == ["bullet", "code"]
        bullet, code = blocks
        assert bullet.text == "Use this template:"
        assert (bullet.start_line, bullet.end_line) == (1, 1)
        assert "rm -rf build/" in code.text
        assert (code.start_line, code.end_line) == (2, 5)

    def test_bullet_nested_fence_contents_never_become_sentences(self) -> None:
        doc = (
            "Retry exactly 5 times on flaky tests.\n"
            "\n"
            "- Example config:\n"
            "  ```yaml\n"
            "  # retry exactly 3 times\n"
            "  retries: 3\n"
            "  ```\n"
        )
        texts = [s.text for s in iter_sentences(parse_blocks(doc))]
        assert "Retry exactly 5 times on flaky tests." in texts
        assert not any("3 times" in t for t in texts)
        assert not any("retries" in t for t in texts)


# ---------------------------------------------------------------------------
# Finding 14: any line CONTAINING ``` closed a fence (substring test)
# ---------------------------------------------------------------------------


class TestFenceCloseRequiresMarkerLine:
    def test_line_mentioning_backticks_does_not_close_fence(self) -> None:
        doc = "```\ncode line\nuse ``` to close fences\nreal code continues\n```\nAfter text.\n"
        blocks = parse_blocks(doc)
        assert [b.kind for b in blocks] == ["code", "paragraph"]
        code, para = blocks
        assert (code.start_line, code.end_line) == (1, 5)
        assert "real code continues" in code.text
        assert para.text == "After text."
        # the prose after the real close is analyzed, the code never is
        assert [s.text for s in iter_sentences(blocks)] == ["After text."]

    def test_longer_opener_run_not_closed_by_shorter_run(self) -> None:
        doc = "````markdown\n```\ninner\n```\n````\nProse after.\n"
        blocks = parse_blocks(doc)
        assert [b.kind for b in blocks] == ["code", "paragraph"]
        assert (blocks[0].start_line, blocks[0].end_line) == (1, 5)
        assert blocks[1].text == "Prose after."


# ---------------------------------------------------------------------------
# Finding 4: pragma lines were body-relative in frontmattered files
# ---------------------------------------------------------------------------


class TestSuppressionLinesAreFileAbsolute:
    RULE = (
        "---\n"
        "title: style\n"
        "owner: me\n"
        "team: infra\n"
        "version: 1\n"
        "---\n"
        "<!-- detangle-ignore DTC01: keeping both until the Q3 style migration -->\n"
        "Always add type hints to new functions.\n"
        "Never add type hints to new functions.\n"
    )

    def test_pragma_line_offset_by_body_start(self, tmp_path: Path) -> None:
        write(tmp_path, ".claude/rules/style.md", self.RULE)
        sups, warnings = collect_suppressions(discover(Config(root=tmp_path)))
        assert warnings == []
        (sup,) = [s for s in sups if s.code == "DTC01"]
        # file-absolute line 7 (after the 6-line frontmatter), not body line 1
        assert sup.line == 7

    def test_pragma_covers_finding_below_it_despite_frontmatter(self, tmp_path: Path) -> None:
        write(tmp_path, ".claude/rules/style.md", self.RULE)
        sups, _ = collect_suppressions(discover(Config(root=tmp_path)))
        finding = Finding(
            code="DTC01",
            message="seeded contradiction",
            severity=Severity.ERROR,
            evidence=[
                Evidence(SourceSpan(".claude/rules/style.md", 8, 8), "Always add type hints"),
                Evidence(SourceSpan(".claude/rules/style.md", 9, 9), "Never add type hints"),
            ],
        )
        assert any(s.covers(finding) for s in sups)

    def test_missing_reason_warning_reports_absolute_line(self, tmp_path: Path) -> None:
        write(
            tmp_path,
            ".claude/rules/r.md",
            "---\na: 1\nb: 2\n---\n<!-- detangle-ignore DTC01 -->\nAlways lint.\n",
        )
        sups, warnings = collect_suppressions(discover(Config(root=tmp_path)))
        assert sups[0].line == 5
        assert any(".claude/rules/r.md:5" in w for w in warnings)


# ---------------------------------------------------------------------------
# Finding 27: Cursor's loose (invalid-YAML) .mdc frontmatter was dropped
# ---------------------------------------------------------------------------

_LOOSE_MDC = (
    "---\n"
    "description: React rules\n"
    "globs: *.tsx,*.ts\n"
    "alwaysApply: false\n"
    "---\n"
    "Use functional components.\n"
)


class TestLooseMdcFrontmatter:
    def test_unquoted_globs_survive_yaml_failure(self) -> None:
        fm, body, start = split_frontmatter(_LOOSE_MDC)
        assert fm["globs"] == "*.tsx,*.ts"
        assert fm["description"] == "React rules"
        assert fm["alwaysApply"] is False
        assert body == "Use functional components.\n"
        assert start == 6

    def test_mdc_with_unquoted_globs_is_auto_attached(self, tmp_path: Path) -> None:
        write(tmp_path, ".cursor/rules/react.mdc", _LOOSE_MDC)
        cf = by_path(discover(Config(root=tmp_path)))[".cursor/rules/react.mdc"]
        assert cf.activation.mode == ActivationMode.PATH
        assert cf.activation.globs == ("*.tsx", "*.ts")

    def test_unrecoverable_frontmatter_still_yields_empty_dict(self) -> None:
        # structural values are not plain scalars — precision-first, skip them
        fm, _body, _start = split_frontmatter("---\nname: [unclosed\n---\nBody.\n")
        assert fm == {}


# ---------------------------------------------------------------------------
# Finding 34: @imports were followed inside inline code and 4-backtick fences
# ---------------------------------------------------------------------------


class TestImportsSkipCode:
    def test_inline_code_span_import_not_followed(self, tmp_path: Path) -> None:
        write(tmp_path, "CLAUDE.md", "run `cat @secret.md` for details\n")
        write(tmp_path, "secret.md", "Never reveal this.\n")
        corpus = discover(Config(root=tmp_path))
        assert "secret.md" not in by_path(corpus)

    def test_import_inside_four_backtick_fence_not_followed(self, tmp_path: Path) -> None:
        write(
            tmp_path,
            "CLAUDE.md",
            "````markdown\n```\nSee @target.md here\n```\n````\nProse.\n",
        )
        write(tmp_path, "target.md", "Should stay out of the corpus.\n")
        corpus = discover(Config(root=tmp_path))
        assert "target.md" not in by_path(corpus)

    def test_fenced_example_missing_target_yields_no_note(self, tmp_path: Path) -> None:
        write(tmp_path, "CLAUDE.md", "````\n```\n@missing-example.md\n```\n````\n")
        root_cf = by_path(discover(Config(root=tmp_path)))["CLAUDE.md"]
        assert not any("missing-example.md" in n for n in root_cf.notes)


# ---------------------------------------------------------------------------
# Finding 35: ./ and ../ @imports were silently ignored; absolute ones unnoted
# ---------------------------------------------------------------------------


class TestRelativeAndAbsoluteImports:
    def test_parent_relative_import_followed(self, tmp_path: Path) -> None:
        write(tmp_path, ".claude/CLAUDE.md", "See @../shared.md for shared rules.\n")
        write(tmp_path, "shared.md", "Never use tabs.\n")
        files = by_path(discover(Config(root=tmp_path)))
        assert "shared.md" in files
        assert files["shared.md"].meta["imported_by"] == ".claude/CLAUDE.md"

    def test_dot_slash_import_followed(self, tmp_path: Path) -> None:
        write(tmp_path, "CLAUDE.md", "See @./docs/a.md for details.\n")
        write(tmp_path, "docs/a.md", "Always lint.\n")
        files = by_path(discover(Config(root=tmp_path)))
        assert "docs/a.md" in files
        assert files["docs/a.md"].meta["imported_by"] == "CLAUDE.md"

    def test_absolute_import_noted_not_followed(self, tmp_path: Path) -> None:
        write(tmp_path, "CLAUDE.md", "Load @/etc/agent/rules.md at start.\n")
        corpus = discover(Config(root=tmp_path))
        files = by_path(corpus)
        assert not any(p.endswith("rules.md") for p in files)
        assert any(
            "@/etc/agent/rules.md" in n and "not followed" in n for n in files["CLAUDE.md"].notes
        )

    def test_parent_import_escaping_repo_is_noted(self, tmp_path: Path) -> None:
        write(tmp_path, "CLAUDE.md", "See @../outside.md too.\n")
        root_cf = by_path(discover(Config(root=tmp_path)))["CLAUDE.md"]
        assert any("outside repo" in n and "@../outside.md" in n for n in root_cf.notes)
