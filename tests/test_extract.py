"""Tests for detangle.extract: modality, conditions, quantities, frames, units."""

from __future__ import annotations

import pytest

from detangle.extract import (
    detect_modality,
    extract_defined_terms,
    extract_frame,
    extract_quantities,
    looks_like_instruction,
    normalize_declarative,
    split_condition,
)
from detangle.ir import Frame, Modality, Strength

# ---------------------------------------------------------------------------
# detect_modality — all six lexicon classes
# ---------------------------------------------------------------------------


class TestDetectModality:
    @pytest.mark.parametrize(
        ("text", "modality", "strength"),
        [
            # FORBID_HARD
            ("Never push to main.", Modality.FORBID, Strength.HARD),
            ("You must not delete files.", Modality.FORBID, Strength.HARD),
            ("Do not commit secrets.", Modality.FORBID, Strength.HARD),
            ("Don't force-push.", Modality.FORBID, Strength.HARD),
            ("Direct deploys are forbidden.", Modality.FORBID, Strength.HARD),
            ("No pushing to main.", Modality.FORBID, Strength.HARD),
            # FORBID_SOFT
            ("Avoid using global variables.", Modality.FORBID, Strength.SOFT),
            ("Refrain from committing secrets.", Modality.FORBID, Strength.SOFT),
            ("You should not inline styles.", Modality.FORBID, Strength.SOFT),
            # OBLIGE_HARD
            ("Always run the tests.", Modality.OBLIGE, Strength.HARD),
            ("You must sign commits.", Modality.OBLIGE, Strength.HARD),
            ("Make sure the build passes.", Modality.OBLIGE, Strength.HARD),
            ("Ensure the linter is green.", Modality.OBLIGE, Strength.HARD),
            # OBLIGE_SOFT
            ("You should keep functions short.", Modality.OBLIGE, Strength.SOFT),
            ("Try to keep PRs small.", Modality.OBLIGE, Strength.SOFT),
            ("Aim to reply within a day.", Modality.OBLIGE, Strength.SOFT),
            # PERMIT
            ("You may skip the changelog.", Modality.PERMIT, Strength.SOFT),
            ("Feel free to refactor.", Modality.PERMIT, Strength.SOFT),
            ("It's ok to ask questions.", Modality.PERMIT, Strength.SOFT),
            # PREFER
            ("Prefer tabs over spaces.", Modality.PREFER, Strength.SOFT),
            ("Default to JSON output.", Modality.PREFER, Strength.SOFT),
            ("Favor composition over inheritance.", Modality.PREFER, Strength.SOFT),
        ],
    )
    def test_lexicon_classes(self, text, modality, strength):
        hit = detect_modality(text)
        assert hit is not None
        assert hit.modality is modality
        assert hit.strength is strength

    def test_no_modality_returns_none(self):
        assert detect_modality("This has plain descriptive words.") is None
        assert detect_modality("") is None

    def test_earliest_match_wins_forbid_before_oblige(self):
        # 'never' at 0 beats the later 'should'
        hit = detect_modality("Never say you should do it.")
        assert (hit.modality, hit.strength) == (Modality.FORBID, Strength.HARD)
        assert hit.pos == 0

    def test_compound_should_never_is_forbid(self):
        # polarity is sacred: 'should never' is a prohibition, not an
        # obligation ('should'@4 must not beat 'never'@11)
        hit = detect_modality("You should never force-push.")
        assert (hit.modality, hit.strength) == (Modality.FORBID, Strength.SOFT)

    def test_compound_always_avoid_is_forbid(self):
        hit = detect_modality("Always avoid the temptation.")
        assert (hit.modality, hit.strength) == (Modality.FORBID, Strength.HARD)

    def test_compound_can_never_is_forbid(self):
        hit = detect_modality("You can never delete production data.")
        assert (hit.modality, hit.strength) == (Modality.FORBID, Strength.HARD)

    def test_emphasis_prefix_is_not_modality(self):
        hit = detect_modality("IMPORTANT: never delete user data.")
        assert (hit.modality, hit.strength) == (Modality.FORBID, Strength.HARD)

    def test_earliest_match_wins_avoid_before_always(self):
        hit = detect_modality("Avoid always doing that.")
        assert (hit.modality, hit.strength) == (Modality.FORBID, Strength.SOFT)

    def test_tie_broken_by_table_order(self):
        # 'must not' and 'must' both match at the same offset; FORBID_HARD is
        # scanned first, so the tie goes to forbid.
        hit = detect_modality("You must not delete files.")
        assert (hit.modality, hit.strength) == (Modality.FORBID, Strength.HARD)

    def test_case_insensitive(self):
        hit = detect_modality("NEVER push to main.")
        assert hit.modality is Modality.FORBID


# ---------------------------------------------------------------------------
# split_condition
# ---------------------------------------------------------------------------


class TestSplitCondition:
    def test_leading_when(self):
        body, cond = split_condition("When editing legacy files, never change signatures")
        assert body == "never change signatures"
        assert cond == "When editing legacy files"

    def test_leading_if(self):
        body, cond = split_condition("If you are unsure, ask the user.")
        assert body == "ask the user."
        assert cond == "If you are unsure"

    def test_leading_before_with_colon(self):
        body, cond = split_condition("Before releasing: bump the version")
        assert body == "bump the version"
        assert cond == "Before releasing"

    def test_leading_for(self):
        body, cond = split_condition("For Python files, use snake_case names")
        assert body == "use snake_case names"
        assert cond == "For Python files"

    def test_trailing_unless(self):
        body, cond = split_condition(
            "Always run tests before committing, unless the change is docs-only."
        )
        assert body == "Always run tests before committing"
        assert cond == "unless the change is docs-only"

    def test_trailing_when(self):
        body, cond = split_condition("Use spaces when writing Python code")
        assert body == "Use spaces"
        assert cond == "when writing Python code"

    def test_trailing_whenever(self):
        body, cond = split_condition("Keep commits atomic whenever you touch shared code")
        assert body == "Keep commits atomic"
        assert cond == "whenever you touch shared code"

    def test_trailing_unless_no_comma(self):
        body, cond = split_condition("Do not rebase shared branches unless the user asks")
        assert body == "Do not rebase shared branches"
        assert cond == "unless the user asks"

    def test_no_condition(self):
        assert split_condition("Never push to main.") == ("Never push to main.", "")

    def test_short_body_trailing_form_not_split(self):
        # Deliberate conservatism: the trailing-condition regex requires a body
        # of at least 8 characters, so a degenerate 'Do X unless Y' stays whole.
        assert split_condition("Do X unless Y") == ("Do X unless Y", "")

    def test_input_stripped(self):
        body, cond = split_condition("  Never push to main.  ")
        assert body == "Never push to main."
        assert cond == ""


# ---------------------------------------------------------------------------
# extract_quantities
# ---------------------------------------------------------------------------


class TestExtractQuantities:
    def test_at_most_three_retries(self):
        (q,) = extract_quantities("Use at most 3 retries.")
        assert q.comparator == "<="
        assert q.value == 3.0
        assert q.unit == "retries"
        assert q.subject == "retries"
        assert q.raw == "at most 3 retries"

    def test_exactly_five_times(self):
        (q,) = extract_quantities("Retry exactly 5 times.")
        assert (q.comparator, q.value, q.unit) == ("==", 5.0, "times")

    def test_word_number_without_comparator_skipped(self):
        # precision gate: word-numbers need an explicit comparator
        # ("stays on one line" / "wait three seconds" are not constraints)
        assert extract_quantities("Wait three seconds between attempts.") == []

    def test_word_number_with_comparator_extracted(self):
        (q,) = extract_quantities("Retry at most three times.")
        assert (q.comparator, q.value, q.unit) == ("<=", 3.0, "times")

    def test_version_string_not_extracted(self):
        assert extract_quantities("Use version 1.2.3 of the tool.") == []

    def test_version_prefix_not_extracted(self):
        assert extract_quantities("Pin to v2 always.") == []

    def test_bare_number_defaults_to_equality(self):
        (q,) = extract_quantities("Allow 3 retries.")
        assert (q.comparator, q.value, q.unit, q.subject) == ("==", 3.0, "retries", "retries")

    def test_under_maps_to_strict_less(self):
        (q,) = extract_quantities("Keep lines under 100 characters.")
        assert (q.comparator, q.value, q.unit) == ("<", 100.0, "chars")

    def test_no_more_than_maps_to_lte(self):
        (q,) = extract_quantities("No more than 10 files per PR.")
        assert (q.comparator, q.value, q.unit) == ("<=", 10.0, "files")

    def test_at_least_maps_to_gte(self):
        (q,) = extract_quantities("Use at least 2 reviewers.")
        assert (q.comparator, q.value) == (">=", 2.0)
        # 'reviewers' is not a known unit alias; the field stays empty (unknown)
        assert q.unit == ""

    def test_decimal_number_with_unit_alias(self):
        (q,) = extract_quantities("Split into 2.5 second chunks.")
        assert (q.comparator, q.value, q.unit) == ("==", 2.5, "seconds")

    def test_no_numbers_no_quantities(self):
        assert extract_quantities("Keep responses concise.") == []


# ---------------------------------------------------------------------------
# extract_frame
# ---------------------------------------------------------------------------


def _frame_for(text: str) -> Frame:
    body, _cond = split_condition(text)
    return extract_frame(body, detect_modality(text))


class TestExtractFrame:
    def test_never_push_directly_to_main(self):
        f = _frame_for("Never push directly to main.")
        assert f.modality is Modality.FORBID
        assert f.strength is Strength.HARD
        assert f.negated is True
        assert f.action == "push"
        assert f.raw_verb == "push"
        # adverb 'directly' and connector 'to' are skipped
        assert f.obj == "main"

    def test_use_tabs_for_indentation(self):
        f = _frame_for("Use tabs for indentation.")
        assert f.modality is Modality.OBLIGE
        assert f.negated is False
        assert f.action == "use"
        assert f.obj.startswith("tabs")
        assert f.obj == "tabs indentation"

    def test_modal_prefix_stripped_before_verb(self):
        f = _frame_for("You must write tests for every change.")
        assert f.action == "write"
        assert f.obj == "tests every change"

    def test_no_gerund_pattern(self):
        f = _frame_for("No pushing to main.")
        assert f.modality is Modality.FORBID
        assert f.negated is True
        # gerund normalized to the verb lemma
        assert f.raw_verb == "pushing"
        assert f.action == "push"
        assert f.obj == "main"

    def test_gerund_after_avoid_is_normalized(self):
        f = _frame_for("Avoid adding new dependencies.")
        assert f.modality is Modality.FORBID
        assert f.strength is Strength.SOFT
        assert f.action == "add"
        assert f.obj == "new dependencies"

    def test_object_stops_at_conditional_connective(self):
        f = _frame_for("Always run the linter before committing.")
        assert f.action == "run"
        assert f.obj == "linter"

    def test_none_hit_keeps_default_modality(self):
        f = extract_frame("run the linter", None)
        assert f.modality is Modality.OBLIGE  # dataclass default
        assert f.negated is False
        assert f.action == "run"

    def test_empty_body(self):
        f = extract_frame("", None)
        assert f.action == ""
        assert f.obj == ""


# ---------------------------------------------------------------------------
# normalize_declarative
# ---------------------------------------------------------------------------


def _normalize(text: str) -> str:
    body, cond = split_condition(text)
    frame = extract_frame(body, detect_modality(text))
    frame.condition = cond
    return normalize_declarative(text, frame)


class TestNormalizeDeclarative:
    def test_forbid_hard(self):
        assert _normalize("Never push to main.") == "The agent must not push to main."

    def test_do_not_form(self):
        assert _normalize("Do not commit secrets!") == "The agent must not commit secrets."

    def test_oblige_hard(self):
        assert _normalize("Always run tests.") == "The agent must run tests."

    def test_oblige_soft(self):
        assert _normalize("You should keep replies short.") == (
            "The agent should keep replies short."
        )

    def test_forbid_soft(self):
        assert _normalize("Avoid committing generated files.") == (
            "The agent should not committing generated files."
        )

    def test_permit(self):
        assert _normalize("You may skip docs for trivial changes.") == (
            "The agent may skip docs for trivial changes."
        )

    def test_condition_reattached_at_end(self):
        assert _normalize("When editing legacy files, never change signatures.") == (
            "The agent must not change signatures when editing legacy files."
        )

    def test_already_declarative_passes_through(self):
        assert _normalize("The agent must not push to main.") == (
            "The agent must not push to main."
        )

    def test_always_ends_with_single_period(self):
        for text in ("Never push to main", "Never push to main.", "Never push to main!"):
            out = _normalize(text)
            assert out.endswith(".")
            assert not out.endswith("..")

    def test_empty_text(self):
        assert normalize_declarative("", Frame()) == ""


# ---------------------------------------------------------------------------
# looks_like_instruction
# ---------------------------------------------------------------------------


class TestLooksLikeInstruction:
    @pytest.mark.parametrize(
        "text",
        [
            "Never push to main.",  # modality marker
            "Use tabs.",  # imperative cue verb
            "Run pytest after each change.",
            "Always ask before deleting files.",
            "You write tests first.",  # second-person directive
            "Prefer small commits.",
            "Avoid global state.",
        ],
    )
    def test_positives(self, text):
        assert looks_like_instruction(text, from_bullet=False) is True

    @pytest.mark.parametrize(
        "text",
        [
            "This project contains three packages.",
            "Contains the config",  # non-imperative starter
            "Provides helper functions for parsing.",
            "Describes the architecture.",
            "The build is slow sometimes.",
            "ab",  # too short
            "",
            "42 100",  # no words at all
        ],
    )
    def test_negatives(self, text):
        assert looks_like_instruction(text, from_bullet=False) is False

    def test_bullet_ify_heuristic_only_applies_to_bullets(self):
        assert looks_like_instruction("Minify assets before deploy", from_bullet=True) is True
        assert looks_like_instruction("Minify assets before deploy", from_bullet=False) is False

    def test_second_person_needs_imperative_verb(self):
        assert looks_like_instruction("You write tests first.", from_bullet=False) is True
        assert looks_like_instruction("You wonder about tests.", from_bullet=False) is False


# ---------------------------------------------------------------------------
# extract_defined_terms
# ---------------------------------------------------------------------------


class TestExtractDefinedTerms:
    def test_quoted_means(self):
        terms = extract_defined_terms('"Hotfix" means a direct commit to the release branch.')
        assert terms == ("hotfix",)

    def test_refers_to(self):
        terms = extract_defined_terms("A blessed module refers to anything under src/core.")
        assert terms == ("a blessed module",)

    def test_stands_for(self):
        terms = extract_defined_terms('The term "LGTM" stands for looks good to me.')
        assert terms == ("lgtm",)

    def test_is_defined_as(self):
        terms = extract_defined_terms("Flaky is defined as failing intermittently in CI.")
        assert terms == ("flaky",)

    def test_terms_are_lowercased(self):
        terms = extract_defined_terms('"HotFix" means an emergency patch.')
        assert terms == ("hotfix",)

    def test_no_definitions(self):
        assert extract_defined_terms("Nothing defined here.") == ()
        assert extract_defined_terms("") == ()
