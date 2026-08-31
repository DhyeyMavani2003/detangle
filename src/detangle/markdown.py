"""Markdown utilities: frontmatter, structure-aware segmentation with line spans.

We deliberately do not depend on a markdown AST library: agent config files
are simple enough (headings, bullets, paragraphs, fenced code) that a
line-oriented scanner is more robust and keeps exact line spans, which are
the currency of every finding.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import yaml

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n(?:---|\.\.\.)\s*(?:\n|\Z)", re.DOTALL)

# Activation-bearing keys worth salvaging from frontmatter that is not valid
# YAML (Cursor itself writes ``globs: *.tsx,*.ts`` in .mdc files — an
# undefined-alias error to a YAML parser, but real activation semantics).
_FM_SALVAGE_KEYS = (
    "alwaysApply",
    "always_apply",
    "applyTo",
    "description",
    "globs",
    "name",
    "paths",
)
_FM_SALVAGE_RE = re.compile(
    r"^(?P<key>" + "|".join(_FM_SALVAGE_KEYS) + r")\s*:\s*(?P<val>\S.*?)\s*$"
)


def _frontmatter_fallback(raw: str) -> dict[str, Any]:
    """Line-wise salvage of simple ``key: value`` scalars from invalid YAML.

    Only well-known activation keys are recovered, and only when the value
    looks like a plain scalar — precision-first: structural values are left
    alone, so truly malformed frontmatter still reads as empty.
    """
    out: dict[str, Any] = {}
    for line in raw.split("\n"):
        m = _FM_SALVAGE_RE.match(line)
        if not m:
            continue
        key, val = m.group("key"), m.group("val")
        if val[0] in "[{|>":
            continue  # YAML flow/block structure, not a plain scalar
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        if key in ("alwaysApply", "always_apply") and val.lower() in ("true", "false"):
            out[key] = val.lower() == "true"
        elif val:
            out[key] = val
    return out


def split_frontmatter(text: str) -> tuple[dict[str, Any], str, int]:
    """Return (frontmatter dict, body, body_start_line 1-based).

    Malformed YAML yields the salvageable simple ``key: value`` scalars
    (Cursor's own loose .mdc frontmatter is not strict YAML), or an empty
    dict when nothing is recoverable.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text, 1
    raw = m.group(1)
    body = text[m.end() :]
    body_start = text[: m.end()].count("\n") + 1
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError:
        return _frontmatter_fallback(raw), body, body_start
    if not isinstance(data, dict):
        data = {}
    return data, body, body_start


@dataclass
class Block:
    """A structural block of a markdown document."""

    kind: str  # "heading" | "bullet" | "paragraph" | "code" | "table" | "comment"
    text: str
    start_line: int  # 1-based
    end_line: int
    heading_path: tuple[str, ...] = ()  # enclosing headings, outermost first
    level: int = 0  # heading level or bullet indent depth


_BULLET_RE = re.compile(r"^(\s*)(?:[-*+]|\d{1,3}[.)])\s+(.*)$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_COMMENT_RE = re.compile(r"<!--(.*?)-->", re.DOTALL)
_FENCE_RE = re.compile(r"^(\s*)(```+|~~~+)")


def _fence_close_re(marker: str) -> re.Pattern[str]:
    """A closing fence is the marker alone on its line (same or longer run)."""
    return re.compile(rf"^\s*{re.escape(marker[0])}{{{len(marker)},}}\s*$")


def parse_blocks(text: str, start_line: int = 1) -> list[Block]:
    """Scan markdown into blocks with line spans and heading context."""
    lines = text.split("\n")
    blocks: list[Block] = []
    heading_stack: list[tuple[int, str]] = []  # (level, title)

    def hpath() -> tuple[str, ...]:
        return tuple(t for _, t in heading_stack)

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        lineno = start_line + i
        stripped = line.strip()

        if not stripped:
            i += 1
            continue

        # fenced code
        fence = _FENCE_RE.match(line)
        if fence:
            close = _fence_close_re(fence.group(2))
            j = i + 1
            while j < n and not close.match(lines[j]):
                j += 1
            blocks.append(
                Block(
                    "code",
                    "\n".join(lines[i : min(j + 1, n)]),
                    lineno,
                    start_line + min(j, n - 1),
                    hpath(),
                )
            )
            i = j + 1
            continue

        m = _HEADING_RE.match(line)
        if m:
            level = len(m.group(1))
            title = m.group(2).strip()
            while heading_stack and heading_stack[-1][0] >= level:
                heading_stack.pop()
            heading_stack.append((level, title))
            blocks.append(Block("heading", title, lineno, lineno, hpath()[:-1], level))
            i += 1
            continue

        m = _BULLET_RE.match(line)
        if m:
            indent = len(m.group(1))
            body = [m.group(2)]
            j = i + 1
            # continuation lines: more-indented, non-bullet, non-blank
            while j < n:
                nxt = lines[j]
                if not nxt.strip():
                    break
                if _BULLET_RE.match(nxt) or _HEADING_RE.match(nxt):
                    break
                if _FENCE_RE.match(nxt):
                    break  # nested fence: let the main loop emit it as code
                if len(nxt) - len(nxt.lstrip()) <= indent:
                    break
                body.append(nxt.strip())
                j += 1
            blocks.append(
                Block("bullet", " ".join(body), lineno, start_line + j - 1, hpath(), indent)
            )
            i = j
            continue

        if _TABLE_ROW_RE.match(line):
            j = i
            while j < n and _TABLE_ROW_RE.match(lines[j]):
                j += 1
            blocks.append(
                Block("table", "\n".join(lines[i:j]), lineno, start_line + j - 1, hpath())
            )
            i = j
            continue

        # paragraph: consume until blank/structural line
        j = i
        para: list[str] = []
        while j < n:
            nxt = lines[j]
            if not nxt.strip():
                break
            if _HEADING_RE.match(nxt) or _BULLET_RE.match(nxt) or _TABLE_ROW_RE.match(nxt):
                break
            if _FENCE_RE.match(nxt):
                break
            para.append(nxt.strip())
            j += 1
        blocks.append(Block("paragraph", " ".join(para), lineno, start_line + j - 1, hpath()))
        i = j

    return blocks


@dataclass
class Sentence:
    text: str
    start_line: int
    end_line: int
    heading_path: tuple[str, ...] = ()
    from_bullet: bool = False


_ABBREV = {"e.g", "i.e", "etc", "vs", "cf", "no", "st", "dr", "mr", "mrs", "ms", "approx"}
# split on sentence enders followed by space+capital/quote/digit — conservative
_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'`(\[0-9])")


def split_sentences(text: str) -> list[str]:
    """Conservative sentence splitter tuned for instruction prose."""
    text = " ".join(text.split())
    if not text:
        return []
    parts: list[str] = []
    buf = ""
    for chunk in _SENT_SPLIT_RE.split(text):
        if buf:
            last_word = buf.rstrip(".!?").rsplit(" ", 1)[-1].lower()
            if last_word in _ABBREV or re.search(r"\b\w\.\w\.$", buf):
                buf = buf + " " + chunk
                continue
            parts.append(buf)
        buf = chunk
    if buf:
        parts.append(buf)
    return [p.strip() for p in parts if p.strip()]


def iter_sentences(blocks: list[Block]) -> list[Sentence]:
    """Instruction-bearing sentences from parsed blocks.

    Code blocks and tables are skipped (they are data, not prose); HTML
    comments are removed from prose (Claude Code strips them before the
    model sees them) but scanned separately by the DTX01 detector.
    """
    out: list[Sentence] = []
    for b in blocks:
        if b.kind in {"code", "table", "heading", "comment"}:
            continue
        prose = _COMMENT_RE.sub(" ", b.text)
        # strip markdown emphasis so ** and * never enter frames as tokens
        prose = re.sub(r"\*\*([^*]+)\*\*", r"\1", prose)
        prose = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"\1", prose)
        prose = re.sub(r"__([^_]+)__", r"\1", prose)
        # keep inline-code content, drop the backticks; protect dots inside
        prose = re.sub(
            r"`([^`]*)`", lambda m: m.group(1).replace(". ", "․ ").replace(" ", "␣"), prose
        )
        sentences = split_sentences(prose)
        for s in sentences:
            s = s.replace("␣", " ").replace("․ ", ". ")
            if len(s) < 3:
                continue
            out.append(Sentence(s, b.start_line, b.end_line, b.heading_path, b.kind == "bullet"))
    return out


def find_comments(text: str) -> list[tuple[str, int, int]]:
    """All HTML comments with their 1-based line spans (for DTX01/suppressions)."""
    res: list[tuple[str, int, int]] = []
    for m in _COMMENT_RE.finditer(text):
        start = text[: m.start()].count("\n") + 1
        end = text[: m.end()].count("\n") + 1
        res.append((m.group(1).strip(), start, end))
    return res
