"""Corpus-hygiene detectors: DTX01 hidden text, DTR05 stale refs, DTP06
unreachable instructions, DTR04 lint leakage.

These run over files and units directly (not candidate pairs).
"""

from __future__ import annotations

import re

from ..findings import Evidence, Finding
from ..ir import ActivationMode, BudgetRisk, SourceSpan
from ..markdown import find_comments
from ..taxonomy import Severity
from .base import AnalysisContext, Detector

# ---------------------------------------------------------------------------
# DTX01 — hidden instructions
# ---------------------------------------------------------------------------

_INVISIBLE_CHARS = {
    "​": "ZERO WIDTH SPACE",
    "‌": "ZERO WIDTH NON-JOINER",
    "‍": "ZERO WIDTH JOINER",
    "⁠": "WORD JOINER",
    "﻿": "ZERO WIDTH NO-BREAK SPACE (BOM)",
    "‎": "LEFT-TO-RIGHT MARK",
    "‏": "RIGHT-TO-LEFT MARK",
    "‪": "LEFT-TO-RIGHT EMBEDDING",
    "‫": "RIGHT-TO-LEFT EMBEDDING",
    "‬": "POP DIRECTIONAL FORMATTING",
    "‭": "LEFT-TO-RIGHT OVERRIDE",
    "‮": "RIGHT-TO-LEFT OVERRIDE",
    "⁦": "LEFT-TO-RIGHT ISOLATE",
    "⁧": "RIGHT-TO-LEFT ISOLATE",
    "⁨": "FIRST STRONG ISOLATE",
    "⁩": "POP DIRECTIONAL ISOLATE",
}
_TAG_CHAR_RE = re.compile(r"[\U000e0000-\U000e007f]")

_COMMENT_PAYLOAD_RE = re.compile(
    r"\b(ignore\s+(?:all\s+)?(?:previous|prior|above)|you\s+must|always\s+|never\s+|"
    r"do\s+not\s+tell|hide\s+|don['’]t\s+mention|system\s*:|absolute\s+authority|"
    r"override\s+|exfiltrate|secret(?:ly)?\s)",
    re.IGNORECASE,
)


class HiddenInstructionDetector(Detector):
    codes = ("DTX01",)
    name = "hidden-instructions"

    def run(self, ctx: AnalysisContext) -> list[Finding]:
        out: list[Finding] = []
        for cf in ctx.corpus.files:
            text = cf.text
            # invisible / directional Unicode
            found: dict[str, list[int]] = {}
            for i, line in enumerate(text.split("\n"), start=1):
                for ch, name in _INVISIBLE_CHARS.items():
                    if ch in line:
                        found.setdefault(name, []).append(i)
                if _TAG_CHAR_RE.search(line):
                    found.setdefault("Unicode TAG characters (invisible text)", []).append(i)
            for name, lines in sorted(found.items()):
                first = lines[0]
                out.append(
                    Finding(
                        code="DTX01",
                        message=(
                            f"Invisible Unicode ({name}) in a config file — a channel "
                            f"for instructions the reviewer cannot see "
                            f"(lines {', '.join(map(str, lines[:5]))}"
                            + ("…" if len(lines) > 5 else "")
                            + ")."
                        ),
                        severity=Severity.ERROR,
                        evidence=[
                            Evidence(
                                SourceSpan(cf.path, first, first),
                                text.split("\n")[first - 1][:120].strip() or "(invisible)",
                                name,
                            )
                        ],
                        suggestion="Strip the characters; if intentional, document why.",
                    )
                )
            # HTML comments carrying imperative payloads. Claude Code strips
            # comments before the model sees CLAUDE.md, but other tools do
            # not — and a directive hidden in a comment is invisible in
            # rendered review either way.
            for body, start, end in find_comments(text):
                if len(body) > 10 and _COMMENT_PAYLOAD_RE.search(body):
                    if re.match(r"^\s*detangle-", body):
                        continue  # our own suppression pragmas
                    out.append(
                        Finding(
                            code="DTX01",
                            message=(
                                "An HTML comment contains directive-style text — "
                                "instructions hidden from rendered review."
                            ),
                            severity=Severity.WARNING,
                            evidence=[
                                Evidence(
                                    SourceSpan(cf.path, start, end),
                                    body[:160],
                                    "comment payload",
                                )
                            ],
                            suggestion=(
                                "Move real instructions into visible prose; delete the "
                                "comment otherwise."
                            ),
                            confidence=0.7,
                        )
                    )
        return out


# ---------------------------------------------------------------------------
# DTR05 — stale references
# ---------------------------------------------------------------------------

_PATH_RE = re.compile(
    r"(?<![\w/])((?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,8}|"
    r"[A-Za-z0-9_.-]+\.(?:py|ts|tsx|js|jsx|rs|go|java|rb|md|toml|ya?ml|json|cfg|ini|sh|sql))"
    r"(?![\w/])"
)
_CMD_RE = re.compile(
    r"`((?:npm\s+run|yarn|pnpm(?:\s+run)?|bun\s+run|make|just)\s+[A-Za-z0-9_.:-]+)`"
)

_GENERIC_FILENAMES = {
    "package.json",
    "readme.md",
    "claude.md",
    "agents.md",
    "makefile",
    "pyproject.toml",
    "cargo.toml",
    "go.mod",
    "setup.py",
    "index.js",
    "index.ts",
    "main.py",
    "app.py",
    "config.json",
    "tsconfig.json",
    ".env",
    "dockerfile",
    "example.py",
    "example.ts",
    "foo.py",
    "bar.py",
    "test.py",
    "file.py",
    "script.sh",
    "types.ts",
    "utils.py",
    "utils.ts",
}


class StaleReferenceDetector(Detector):
    codes = ("DTR05",)
    name = "stale-references"

    def run(self, ctx: AnalysisContext) -> list[Finding]:
        out: list[Finding] = []
        repo_files = ctx.corpus.repo_files
        repo_dirs = {p.rsplit("/", 1)[0] for p in repo_files if "/" in p}
        cmds = ctx.corpus.known_commands
        seen: set[tuple[str, str]] = set()

        for u in ctx.units:
            text = u.text
            for m in _PATH_RE.finditer(text):
                ref = m.group(1).strip("./")
                if "*" in ref or "{" in ref:
                    continue
                if ref.lower() in _GENERIC_FILENAMES or "/" not in ref:
                    continue  # bare filenames are too often examples
                if "example" in ref.lower() or ref.lower().startswith(("path/", "your/", "some/")):
                    continue
                if ref in repo_files or ref in repo_dirs:
                    continue
                if any(f.endswith("/" + ref) for f in repo_files):
                    continue
                key = (u.file.path, ref)
                if key in seen:
                    continue
                seen.add(key)
                out.append(
                    Finding(
                        code="DTR05",
                        message=f"References '{ref}', which does not exist in the repository.",
                        severity=Severity.WARNING,
                        evidence=[Evidence(u.span, u.text, f"mentions {ref}")],
                        units=[u],
                        suggestion="Update or remove the reference (the file may have moved).",
                        confidence=0.8,
                    )
                )
            for m in _CMD_RE.finditer(text):
                cmd = " ".join(m.group(1).split())
                if cmds and cmd not in cmds:
                    key = (u.file.path, cmd)
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append(
                        Finding(
                            code="DTR05",
                            message=(
                                f"References the command '{cmd}', which is not defined in "
                                "this repository's scripts/targets."
                            ),
                            severity=Severity.WARNING,
                            evidence=[Evidence(u.span, u.text, f"mentions {cmd}")],
                            units=[u],
                            suggestion="Fix the command name or define the script/target.",
                            confidence=0.8,
                        )
                    )
        return out


# ---------------------------------------------------------------------------
# DTP06 — unreachable / budget-truncated instructions
# ---------------------------------------------------------------------------


class UnreachableDetector(Detector):
    codes = ("DTP06",)
    name = "unreachable"

    def run(self, ctx: AnalysisContext) -> list[Finding]:
        out: list[Finding] = []
        flagged_files: set[str] = set()

        for cf in ctx.corpus.files:
            act = cf.activation
            if act.budget_risk == BudgetRisk.TRUNCATION and cf.path not in flagged_files:
                flagged_files.add(cf.path)
                out.append(
                    Finding(
                        code="DTP06",
                        message=(
                            f"Instructions at risk of never reaching the model: {act.budget_note}."
                        ),
                        severity=Severity.WARNING,
                        evidence=[Evidence(SourceSpan(cf.path, 1, 1), cf.path, "whole file")],
                        suggestion="Trim earlier files or move critical rules above the budget line.",
                    )
                )
            elif act.budget_risk == BudgetRisk.LISTING and cf.path not in flagged_files:
                flagged_files.add(cf.path)
                note = "; ".join(cf.notes) or act.budget_note
                out.append(
                    Finding(
                        code="DTP06",
                        message=f"Trigger description will be truncated: {note}.",
                        severity=Severity.WARNING,
                        evidence=[Evidence(SourceSpan(cf.path, 1, 1), cf.path, "frontmatter")],
                        suggestion=(
                            "Shorten description/when_to_use below the listing cap so the "
                            "model sees the full trigger."
                        ),
                    )
                )

            # dead scope: path-triggered file whose globs match nothing
            if act.mode == ActivationMode.PATH and act.globs:
                from ..globs import any_glob_match

                if not any(any_glob_match(act.globs, rf) for rf in ctx.corpus.repo_files):
                    out.append(
                        Finding(
                            code="DTP06",
                            message=(
                                f"Dead scope: activation globs ({', '.join(act.globs)}) "
                                "match no file in the repository — these instructions "
                                "can never trigger."
                            ),
                            severity=Severity.WARNING,
                            evidence=[
                                Evidence(SourceSpan(cf.path, 1, 1), cf.path, "activation globs")
                            ],
                            suggestion="Fix the glob patterns or delete the rule.",
                        )
                    )

        # parser-detected unreachability (e.g. .md files in .cursor/rules)
        for note in ctx.corpus.notes:
            if "ignored by Cursor" in note or "silently skips" in note:
                path = note.split(":", 1)[0]
                out.append(
                    Finding(
                        code="DTP06",
                        message=note.split(": ", 1)[-1] if ": " in note else note,
                        severity=Severity.WARNING,
                        evidence=[Evidence(SourceSpan(path, 1, 1), path, "")],
                        suggestion="",
                    )
                )
        return out


# ---------------------------------------------------------------------------
# DTR04 — lint leakage (restating what an enforcer already guarantees)
# ---------------------------------------------------------------------------

_ENFORCER_CONFIGS: dict[str, tuple[str, ...]] = {
    "prettier": (
        ".prettierrc",
        ".prettierrc.json",
        ".prettierrc.yml",
        ".prettierrc.yaml",
        "prettier.config.js",
        ".prettierrc.js",
    ),
    "eslint": (
        ".eslintrc",
        ".eslintrc.json",
        ".eslintrc.js",
        ".eslintrc.yml",
        "eslint.config.js",
        "eslint.config.mjs",
    ),
    "black": ("pyproject.toml:black",),
    "ruff": ("pyproject.toml:ruff", "ruff.toml", ".ruff.toml"),
    "gofmt": (),
    "rustfmt": ("rustfmt.toml", ".rustfmt.toml"),
}

_LEAKAGE_RE = re.compile(
    r"\b(?:format(?:ted)?|style|lint(?:ed)?)\b.{0,40}\b(prettier|eslint|black|ruff|gofmt|rustfmt)\b|"
    r"\b(prettier|eslint|black|ruff|gofmt|rustfmt)\b.{0,40}\b(?:format(?:ting)?|style|rules)\b",
    re.IGNORECASE,
)


class LintLeakageDetector(Detector):
    codes = ("DTR04",)
    name = "lint-leakage"

    def run(self, ctx: AnalysisContext) -> list[Finding]:
        out: list[Finding] = []
        repo_files = ctx.corpus.repo_files
        pyproject = ""
        if "pyproject.toml" in repo_files:
            try:
                pyproject = (ctx.corpus.root / "pyproject.toml").read_text(encoding="utf-8")
            except OSError:
                pyproject = ""

        def enforcer_configured(tool: str) -> bool:
            for probe in _ENFORCER_CONFIGS.get(tool, ()):
                if probe.startswith("pyproject.toml:"):
                    section = probe.split(":", 1)[1]
                    if f"[tool.{section}" in pyproject:
                        return True
                elif probe in repo_files:
                    return True
            return False

        for u in ctx.units:
            m = _LEAKAGE_RE.search(u.text)
            if not m:
                continue
            tool = (m.group(1) or m.group(2) or "").lower()
            if not tool or not enforcer_configured(tool):
                continue
            out.append(
                Finding(
                    code="DTR04",
                    message=(
                        f"Restates formatting/style that {tool} already enforces via its "
                        "config in this repo — prose isn't policy; the enforcer is."
                    ),
                    severity=Severity.INFO,
                    evidence=[Evidence(u.span, u.text, f"{tool} is configured")],
                    units=[u],
                    suggestion=(
                        f"Drop the sentence (or replace with 'run {tool}'), and let the "
                        "tool's config be the single source of truth."
                    ),
                    confidence=0.7,
                )
            )
        return out
