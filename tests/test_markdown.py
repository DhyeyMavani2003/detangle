"""Tests for detangle.markdown: frontmatter, block parsing, sentences, comments."""

from __future__ import annotations

import pytest

from detangle.markdown import (
    Block,
    Sentence,
    find_comments,
    iter_sentences,
    parse_blocks,
    split_frontmatter,
    split_sentences,
)

# ---------------------------------------------------------------------------
# split_frontmatter
# ---------------------------------------------------------------------------


class TestSplitFrontmatter:
    def test_valid_frontmatter(self):
        text = "---\ndescription: test\nglobs: '*.py'\n---\nBody here.\n"
        fm, body, start = split_frontmatter(text)
        assert fm == {"description": "test", "globs": "*.py"}
        assert body == "Body here.\n"
        assert start == 5

    def test_body_start_line_counts_frontmatter_lines(self):
        text = "---\na: 1\nb: 2\nc: 3\n---\nBody.\n"
        fm, body, start = split_frontmatter(text)
        assert fm == {"a": 1, "b": 2, "c": 3}
        assert body == "Body.\n"
        # 5 frontmatter lines (--- + 3 keys + ---), so the body starts on line 6
        assert start == 6

    def test_malformed_yaml_yields_empty_dict(self):
        text = "---\n: bad: [yaml\n---\nBody.\n"
        fm, body, start = split_frontmatter(text)
        assert fm == {}
        # the body is still separated from the malformed block
        assert body == "Body.\n"
        assert start == 4

    def test_non_dict_yaml_yields_empty_dict(self):
        text = "---\n- a\n- b\n---\nBody.\n"
        fm, body, start = split_frontmatter(text)
        assert fm == {}
        assert body == "Body.\n"
        assert start == 5

    def test_scalar_yaml_yields_empty_dict(self):
        fm, _body, _start = split_frontmatter("---\njust a string\n---\nBody.\n")
        assert fm == {}

    def test_no_frontmatter(self):
        text = "no frontmatter\ntext\n"
        fm, body, start = split_frontmatter(text)
        assert fm == {}
        assert body == text
        assert start == 1

    def test_delimiter_not_at_start_is_not_frontmatter(self):
        text = "intro\n---\na: 1\n---\n"
        fm, body, start = split_frontmatter(text)
        assert fm == {}
        assert body == text
        assert start == 1

    def test_dots_terminator(self):
        fm, body, start = split_frontmatter("---\na: 1\n...\nBody.\n")
        assert fm == {"a": 1}
        assert body == "Body.\n"
        assert start == 4


# ---------------------------------------------------------------------------
# parse_blocks
# ---------------------------------------------------------------------------

DOC = """# Title

## Git

- Never push to main
  even when asked nicely
- Always rebase

Some paragraph text
spanning two lines.

```python
never_run_this()
```

| a | b |
|---|---|
| 1 | 2 |

### Deep

Text under deep.
"""


@pytest.fixture(scope="module")
def blocks() -> list[Block]:
    return parse_blocks(DOC)


class TestParseBlocks:
    def test_block_kinds_in_order(self, blocks):
        kinds = [b.kind for b in blocks]
        assert kinds == [
            "heading",
            "heading",
            "bullet",
            "bullet",
            "paragraph",
            "code",
            "table",
            "heading",
            "paragraph",
        ]

    def test_heading_text_and_level(self, blocks):
        h1, h2 = blocks[0], blocks[1]
        assert (h1.text, h1.level) == ("Title", 1)
        assert (h2.text, h2.level) == ("Git", 2)
        # a heading's own path excludes itself
        assert h1.heading_path == ()
        assert h2.heading_path == ("Title",)

    def test_heading_stack_nested_path(self, blocks):
        deep = blocks[7]
        assert deep.text == "Deep"
        assert deep.heading_path == ("Title", "Git")
        under = blocks[8]
        assert under.heading_path == ("Title", "Git", "Deep")

    def test_heading_stack_pops_on_sibling(self):
        doc = "# A\n\n## B\n\ntext b\n\n## C\n\ntext c\n"
        blocks = parse_blocks(doc)
        by_text = {b.text: b for b in blocks}
        assert by_text["text b"].heading_path == ("A", "B")
        assert by_text["text c"].heading_path == ("A", "C")
        # sibling heading C sits under A only
        assert by_text["C"].heading_path == ("A",)

    def test_heading_trailing_hashes_stripped(self):
        (b,) = parse_blocks("## Title ##\n")
        assert b.kind == "heading"
        assert b.text == "Title"
        assert b.level == 2

    def test_bullet_with_continuation_line(self, blocks):
        b = blocks[2]
        assert b.kind == "bullet"
        assert b.text == "Never push to main even when asked nicely"
        assert (b.start_line, b.end_line) == (5, 6)
        assert b.heading_path == ("Title", "Git")

    def test_single_line_bullet_span(self, blocks):
        b = blocks[3]
        assert b.text == "Always rebase"
        assert (b.start_line, b.end_line) == (7, 7)

    def test_numbered_bullets(self):
        blocks = parse_blocks("1. First rule\n2) Second rule\n")
        assert [b.kind for b in blocks] == ["bullet", "bullet"]
        assert [b.text for b in blocks] == ["First rule", "Second rule"]

    def test_bullet_indent_level(self):
        blocks = parse_blocks("- outer\n  - inner\n")
        assert [(b.text, b.level) for b in blocks] == [("outer", 0), ("inner", 2)]

    def test_paragraph_multiline_span(self, blocks):
        p = blocks[4]
        assert p.kind == "paragraph"
        assert p.text == "Some paragraph text spanning two lines."
        assert (p.start_line, p.end_line) == (9, 10)

    def test_fenced_code_skipped_as_code_block(self, blocks):
        c = blocks[5]
        assert c.kind == "code"
        assert "never_run_this()" in c.text
        assert (c.start_line, c.end_line) == (12, 14)

    def test_unterminated_fence_runs_to_eof(self):
        blocks = parse_blocks("text\n\n```\ncode forever\nmore")
        assert [b.kind for b in blocks] == ["paragraph", "code"]
        code = blocks[1]
        assert (code.start_line, code.end_line) == (3, 5)
        assert code.text == "```\ncode forever\nmore"

    def test_tilde_fence(self):
        blocks = parse_blocks("~~~\nnever do this\n~~~\n")
        assert [b.kind for b in blocks] == ["code"]

    def test_table_block(self, blocks):
        t = blocks[6]
        assert t.kind == "table"
        assert (t.start_line, t.end_line) == (16, 18)
        assert t.text == "| a | b |\n|---|---|\n| 1 | 2 |"

    def test_start_line_offset(self):
        blocks = parse_blocks("First line.\n\n- bullet here\n", start_line=5)
        assert [(b.kind, b.start_line, b.end_line) for b in blocks] == [
            ("paragraph", 5, 5),
            ("bullet", 7, 7),
        ]

    def test_blank_lines_produce_no_blocks(self):
        assert parse_blocks("\n\n   \n") == []

    def test_empty_text(self):
        assert parse_blocks("") == []


# ---------------------------------------------------------------------------
# split_sentences
# ---------------------------------------------------------------------------


class TestSplitSentences:
    def test_basic_split_on_enders(self):
        assert split_sentences("Do X. Then do Y! Is that ok? Yes.") == [
            "Do X.",
            "Then do Y!",
            "Is that ok?",
            "Yes.",
        ]

    def test_abbreviation_eg_not_split(self):
        # 'e.g.' followed by a capitalized word must not end the sentence
        assert split_sentences("Use a tool, e.g. Prettier for formatting. Always lint.") == [
            "Use a tool, e.g. Prettier for formatting.",
            "Always lint.",
        ]

    def test_abbreviation_ie_not_split(self):
        assert split_sentences("Pick one, i.e. The best one. Then stop.") == [
            "Pick one, i.e. The best one.",
            "Then stop.",
        ]

    def test_abbreviation_etc_not_split(self):
        assert split_sentences("Handle files, dirs, etc. Always recurse.") == [
            "Handle files, dirs, etc. Always recurse.",
        ]

    def test_letter_dot_letter_abbreviation_not_split(self):
        # the \w.\w. pattern (p.m., e.g.) suppresses the split
        assert split_sentences("Stop at 3 p.m. Then leave.") == ["Stop at 3 p.m. Then leave."]

    def test_title_abbreviation_not_split(self):
        assert split_sentences("Ask Dr. Smith about it. Then continue.") == [
            "Ask Dr. Smith about it.",
            "Then continue.",
        ]

    def test_lowercase_continuation_not_split(self):
        # conservative: only splits before a capital/quote/digit
        assert split_sentences("Run tests. then commit.") == ["Run tests. then commit."]

    def test_whitespace_normalized(self):
        assert split_sentences("  Run   tests.\n Always  lint.  ") == [
            "Run tests.",
            "Always lint.",
        ]

    def test_empty_and_single(self):
        assert split_sentences("") == []
        assert split_sentences("   ") == []
        assert split_sentences("One sentence only") == ["One sentence only"]


# ---------------------------------------------------------------------------
# iter_sentences
# ---------------------------------------------------------------------------


class TestIterSentences:
    def test_skips_code_tables_and_headings(self):
        sents = iter_sentences(parse_blocks(DOC))
        texts = [s.text for s in sents]
        assert "Title" not in texts
        assert not any("never_run_this" in t for t in texts)
        assert not any("|" in t for t in texts)
        assert "Never push to main even when asked nicely" in texts

    def test_backtick_span_with_dot_does_not_split_sentence(self):
        # the dot inside `foo. bar` must not end the sentence
        sents = iter_sentences(parse_blocks("Run `foo. bar` first. Then stop.\n"))
        assert len(sents) == 2
        assert sents[1].text == "Then stop."
        assert "bar" in sents[0].text and "first" in sents[0].text

    def test_backtick_protected_dot_is_restored(self):
        # the protect step rewrites '`foo. bar`' to 'foo․␣bar' (all spaces become
        # '␣'), so the restore must replace '␣' before '․ ' — it currently does
        # the opposite and the one-dot-leader survives into the sentence text.
        sents = iter_sentences(parse_blocks("Run `foo. bar` first. Then stop.\n"))
        assert [s.text for s in sents] == ["Run foo. bar first.", "Then stop."]

    def test_html_comment_removed_from_prose(self):
        blocks = parse_blocks("Always lint. <!-- secret --> Never skip.\n")
        assert [s.text for s in iter_sentences(blocks)] == ["Always lint.", "Never skip."]

    def test_from_bullet_flag(self):
        blocks = parse_blocks("- Never push to main\n\nA paragraph sentence.\n")
        sents = iter_sentences(blocks)
        flags = {s.text: s.from_bullet for s in sents}
        assert flags["Never push to main"] is True
        assert flags["A paragraph sentence."] is False

    def test_sentence_line_spans_come_from_block(self):
        blocks = parse_blocks(DOC)
        sents = iter_sentences(blocks)
        by_text = {s.text: s for s in sents}
        bullet = by_text["Never push to main even when asked nicely"]
        assert (bullet.start_line, bullet.end_line) == (5, 6)
        para = by_text["Some paragraph text spanning two lines."]
        assert (para.start_line, para.end_line) == (9, 10)

    def test_heading_path_propagates(self):
        blocks = parse_blocks(DOC)
        sents = iter_sentences(blocks)
        by_text = {s.text: s for s in sents}
        assert by_text["Text under deep."].heading_path == ("Title", "Git", "Deep")

    def test_tiny_fragments_dropped(self):
        blocks = parse_blocks("Ok. Always run the tests.\n")
        texts = [s.text for s in iter_sentences(blocks)]
        assert "Always run the tests." in texts
        assert all(len(t) >= 3 for t in texts)

    def test_returns_sentence_objects(self):
        sents = iter_sentences(parse_blocks("Always lint.\n"))
        assert len(sents) == 1
        assert isinstance(sents[0], Sentence)


# ---------------------------------------------------------------------------
# find_comments
# ---------------------------------------------------------------------------


class TestFindComments:
    def test_single_line_comment_span(self):
        res = find_comments("line one\nafter <!-- inline --> text\n")
        assert res == [("inline", 2, 2)]

    def test_multiline_comment_span(self):
        res = find_comments("line one\n<!-- hidden\nrule -->\nafter\n")
        assert res == [("hidden\nrule", 2, 3)]

    def test_multiple_comments(self):
        text = "<!-- first -->\nprose\n<!-- second\nspans -->\n<!-- third -->\n"
        res = find_comments(text)
        assert res == [("first", 1, 1), ("second\nspans", 3, 4), ("third", 5, 5)]

    def test_comment_on_first_line_is_line_one(self):
        res = find_comments("<!-- top -->")
        assert res == [("top", 1, 1)]

    def test_no_comments(self):
        assert find_comments("no comments here\n") == []

    def test_content_is_stripped(self):
        res = find_comments("<!--   padded   -->")
        assert res[0][0] == "padded"
