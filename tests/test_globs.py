"""Tests for detangle.globs: matching, intersection, subset, and set variants."""

from __future__ import annotations

import pytest

from detangle.globs import (
    any_glob_match,
    expand_braces,
    glob_match,
    glob_set_subset,
    glob_sets_intersect,
    glob_subset,
    globs_intersect,
)

# ---------------------------------------------------------------------------
# expand_braces
# ---------------------------------------------------------------------------


class TestExpandBraces:
    def test_simple_alternation(self):
        assert expand_braces("*.{ts,tsx}") == ["*.ts", "*.tsx"]

    def test_cartesian_product(self):
        assert expand_braces("{a,b}/{c,d}") == ["a/c", "a/d", "b/c", "b/d"]

    def test_no_braces_passthrough(self):
        assert expand_braces("plain") == ["plain"]

    def test_expansion_is_bounded(self):
        pattern = "/".join(["{a,b,c,d}"] * 6)  # 4096 combos if unbounded
        assert len(expand_braces(pattern)) <= 64


# ---------------------------------------------------------------------------
# glob_match
# ---------------------------------------------------------------------------


class TestGlobMatch:
    @pytest.mark.parametrize(
        ("pattern", "path"),
        [
            # ** semantics
            ("**/*.py", "src/a/b/c.py"),
            ("**/*.py", "top.py"),  # '**/' matches zero directories
            ("src/**/*.ts", "src/api/handlers/x.ts"),
            ("src/**/*.ts", "src/x.ts"),
            ("src/**", "src/deep/thing.rs"),
            ("a/**/b", "a/b"),  # middle '**' matches zero segments
            ("a/**/b", "a/x/y/b"),
            # bare pattern matches at any depth (gitignore-ish)
            ("*.py", "mod.py"),
            ("*.py", "deep/nested/mod.py"),
            # dir/ suffix means everything under the dir
            ("docs/", "docs/guide/x.md"),
            # character classes
            ("file[0-9].txt", "file7.txt"),
            ("file[!0-9].txt", "fileX.txt"),
            # single-char wildcard
            ("?at.txt", "cat.txt"),
            # braces
            ("*.{ts,tsx}", "app/main.tsx"),
            # leading-slash / dot-slash normalization
            ("/rooted/*.md", "rooted/x.md"),
            ("./rel/*.md", "rel/x.md"),
            # literal
            ("src/main.py", "src/main.py"),
        ],
    )
    def test_matches(self, pattern, path):
        assert glob_match(pattern, path) is True

    @pytest.mark.parametrize(
        ("pattern", "path"),
        [
            ("src/*.py", "src/a/b.py"),  # single '*' does not cross '/'
            ("file[0-9].txt", "fileX.txt"),
            ("file[!0-9].txt", "file3.txt"),
            ("?at.txt", "at.txt"),  # '?' matches exactly one char
            ("*.{ts,tsx}", "app/main.js"),
            ("*.py", "mod.pyc"),
            ("docs/", "docs"),  # 'docs/' targets contents, not the dir entry
            ("src/**", "src"),
            ("src/main.py", "src/other.py"),
        ],
    )
    def test_non_matches(self, pattern, path):
        assert glob_match(pattern, path) is False

    def test_path_leading_slash_stripped(self):
        assert glob_match("src/*.py", "/src/main.py") is True

    def test_unterminated_char_class_is_literal(self):
        assert glob_match("bad[.txt", "bad[.txt") is True
        assert glob_match("bad[.txt", "badX.txt") is False

    def test_any_glob_match(self):
        assert any_glob_match(("*.py", "*.md"), "docs/x.md") is True
        assert any_glob_match(["*.py", "*.md"], "x.rs") is False
        assert any_glob_match((), "x.rs") is False


# ---------------------------------------------------------------------------
# globs_intersect
# ---------------------------------------------------------------------------


class TestGlobsIntersect:
    def test_star_suffix_and_prefix_overlap(self):
        # both match e.g. 'test_x.py'
        assert globs_intersect("*.py", "test_*") is True

    def test_disjoint_extensions(self):
        assert globs_intersect("*.py", "*.md") is False

    def test_doublestar_within_same_tree(self):
        assert globs_intersect("src/**/*.ts", "src/api/**") is True

    def test_disjoint_directories(self):
        assert globs_intersect("src/**", "docs/**") is False
        assert globs_intersect("a/b/*.js", "a/c/*.js") is False

    def test_anydepth_doublestar_reaches_into_dir(self):
        assert globs_intersect("**/*.py", "docs/**") is True

    def test_pattern_vs_literal(self):
        assert globs_intersect("src/*.py", "src/main.py") is True
        assert globs_intersect("src/*.py", "src/main.md") is False

    def test_brace_expansion_considered(self):
        assert globs_intersect("{a,b}/x", "b/*") is True
        assert globs_intersect("{a,b}/x", "c/*") is False

    def test_bare_pattern_any_depth_in_intersection(self):
        # bare '*.py' also lives under docs/, so the trees overlap
        assert globs_intersect("*.py", "docs/**") is True

    def test_char_class_approximated_as_single_char(self):
        # classes degrade to '?', which can only over-report overlap
        assert globs_intersect("src/[abc]*.py", "src/a*.py") is True

    def test_symmetry(self):
        for a, b in [("*.py", "test_*"), ("src/**/*.ts", "src/api/**"), ("*.py", "*.md")]:
            assert globs_intersect(a, b) == globs_intersect(b, a)

    def test_identical_patterns_intersect(self):
        assert globs_intersect("src/**/*.ts", "src/**/*.ts") is True


class TestGlobSetsIntersect:
    def test_any_pair_suffices(self):
        assert glob_sets_intersect(("*.py", "*.md"), ("docs/**",)) is True

    def test_all_pairs_disjoint(self):
        assert glob_sets_intersect(("*.rs",), ("*.md", "*.py")) is False

    def test_empty_sets_never_intersect(self):
        assert glob_sets_intersect((), ("*.md",)) is False
        assert glob_sets_intersect(("*.md",), ()) is False
        assert glob_sets_intersect((), ()) is False

    def test_accepts_lists(self):
        assert glob_sets_intersect(["src/**"], ["src/api/*.ts"]) is True


# ---------------------------------------------------------------------------
# glob_subset
# ---------------------------------------------------------------------------


class TestGlobSubset:
    def test_narrow_dir_pattern_inside_doublestar(self):
        assert glob_subset("src/api/*.ts", "src/**") is True

    def test_reverse_is_not_subset(self):
        assert glob_subset("src/**", "src/api/*.ts") is False

    def test_identical_patterns(self):
        assert glob_subset("src/**", "src/**") is True
        assert glob_subset("src/api/*.ts", "src/api/*.ts") is True

    def test_everything_covers_bare_star(self):
        assert glob_subset("*.py", "**") is True

    def test_bare_star_does_not_cover_everything(self):
        assert glob_subset("**", "*.py") is False

    def test_rooted_pattern_inside_anydepth(self):
        assert glob_subset("src/*.py", "**/*.py") is True

    def test_dir_tree_inside_everything(self):
        assert glob_subset("docs/**", "**") is True

    def test_brace_superset(self):
        assert glob_subset("*.tsx", "*.{ts,tsx}") is True

    def test_disjoint_patterns_are_not_subset(self):
        assert glob_subset("docs/**", "src/**") is False


class TestGlobSetSubset:
    def test_each_sub_covered_by_some_sup(self):
        assert glob_set_subset(("src/a/*.py", "src/b/*.py"), ("src/**",)) is True

    def test_wider_sub_not_covered(self):
        assert glob_set_subset(("src/**",), ("src/a/*.py",)) is False

    def test_one_uncovered_member_fails(self):
        assert glob_set_subset(("src/*.py", "docs/*.md"), ("src/**",)) is False

    def test_covered_by_combination(self):
        assert glob_set_subset(("*.{ts,tsx}",), ("*.ts", "*.tsx")) is True

    def test_empty_sets_are_never_subset(self):
        assert glob_set_subset((), ("src/**",)) is False
        assert glob_set_subset(("src/**",), ()) is False
        assert glob_set_subset((), ()) is False
