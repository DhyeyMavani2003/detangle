"""Config tests: .detangle.toml discovery, parsing, validation errors."""

from __future__ import annotations

from pathlib import Path

import pytest

from detangle.config import Config, ConfigError, find_config_file, load_config
from detangle.taxonomy import Severity


def write_config(tmp_path: Path, text: str, name: str = ".detangle.toml") -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# defaults & discovery
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_no_file_yields_defaults(self, tmp_path: Path) -> None:
        cfg = load_config(tmp_path)
        assert cfg.root == tmp_path.resolve()
        assert cfg.ecosystems == ("claude-code", "agents-md", "cursor", "copilot")
        assert cfg.lane_nli is False
        assert cfg.lane_jury is False
        assert cfg.include_soft is True
        assert cfg.fail_on == Severity.ERROR
        assert cfg.conflict_budget is None
        assert cfg.disabled_rules == frozenset()
        assert cfg.severity_overrides == {}
        assert cfg.ignore_globs == ()
        assert cfg.respect_gitignore is True

    def test_default_severity_and_enabled_helpers(self) -> None:
        cfg = Config()
        assert cfg.severity_for("DTC01") == Severity.ERROR
        assert cfg.severity_for("DTC08") == Severity.ADVISORY
        assert cfg.severity_for("NOPE") == Severity.WARNING  # unknown code fallback
        assert cfg.rule_enabled("DTC01")


class TestDiscovery:
    def test_finds_dot_detangle_toml(self, tmp_path: Path) -> None:
        p = write_config(tmp_path, "")
        assert find_config_file(tmp_path) == p

    def test_finds_bare_detangle_toml(self, tmp_path: Path) -> None:
        p = write_config(tmp_path, "", name="detangle.toml")
        assert find_config_file(tmp_path) == p

    def test_dotted_name_wins_over_bare(self, tmp_path: Path) -> None:
        write_config(tmp_path, 'fail_on = "info"', name="detangle.toml")
        dotted = write_config(tmp_path, 'fail_on = "warning"')
        assert find_config_file(tmp_path) == dotted
        assert load_config(tmp_path).fail_on == Severity.WARNING

    def test_explicit_path_overrides_discovery(self, tmp_path: Path) -> None:
        write_config(tmp_path, 'fail_on = "warning"')
        other = write_config(tmp_path, 'fail_on = "info"', name="other.toml")
        assert load_config(tmp_path, other).fail_on == Severity.INFO

    def test_no_config_file_found(self, tmp_path: Path) -> None:
        assert find_config_file(tmp_path) is None


# ---------------------------------------------------------------------------
# table shapes: [detangle] vs top-level
# ---------------------------------------------------------------------------


class TestTableShapes:
    def test_detangle_table_accepted(self, tmp_path: Path) -> None:
        write_config(tmp_path, '[detangle]\nfail_on = "warning"\n')
        assert load_config(tmp_path).fail_on == Severity.WARNING

    def test_top_level_accepted(self, tmp_path: Path) -> None:
        write_config(tmp_path, 'fail_on = "warning"\n')
        assert load_config(tmp_path).fail_on == Severity.WARNING

    def test_nested_subtables_under_detangle(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            "[detangle]\n"
            'ecosystems = ["claude-code"]\n'
            "[detangle.lanes]\n"
            "nli = true\n"
            "[detangle.rules]\n"
            "DTC01 = false\n",
        )
        cfg = load_config(tmp_path)
        assert cfg.ecosystems == ("claude-code",)
        assert cfg.lane_nli is True
        assert cfg.disabled_rules == frozenset({"DTC01"})


# ---------------------------------------------------------------------------
# lanes
# ---------------------------------------------------------------------------


class TestLanes:
    def test_lane_booleans(self, tmp_path: Path) -> None:
        write_config(tmp_path, "[lanes]\nnli = true\njury = true\n")
        cfg = load_config(tmp_path)
        assert cfg.lane_nli is True
        assert cfg.lane_jury is True

    def test_lanes_default_off(self, tmp_path: Path) -> None:
        write_config(tmp_path, "[lanes]\n")
        cfg = load_config(tmp_path)
        assert cfg.lane_nli is False
        assert cfg.lane_jury is False

    def test_lanes_must_be_a_table(self, tmp_path: Path) -> None:
        write_config(tmp_path, 'lanes = "on"\n')
        with pytest.raises(ConfigError, match="'lanes' must be a table"):
            load_config(tmp_path)


# ---------------------------------------------------------------------------
# fail_on
# ---------------------------------------------------------------------------


class TestFailOn:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("info", Severity.INFO),
            ("advisory", Severity.ADVISORY),
            ("warning", Severity.WARNING),
            ("error", Severity.ERROR),
            ("WARNING", Severity.WARNING),  # case-insensitive
        ],
    )
    def test_valid_values(self, tmp_path: Path, value: str, expected: Severity) -> None:
        write_config(tmp_path, f'fail_on = "{value}"\n')
        assert load_config(tmp_path).fail_on == expected

    def test_invalid_value_raises(self, tmp_path: Path) -> None:
        write_config(tmp_path, 'fail_on = "fatal"\n')
        with pytest.raises(ConfigError, match="'fail_on' must be one of"):
            load_config(tmp_path)


# ---------------------------------------------------------------------------
# [rules]
# ---------------------------------------------------------------------------


class TestRulesTable:
    def test_false_disables(self, tmp_path: Path) -> None:
        write_config(tmp_path, "[rules]\nDTC01 = false\n")
        cfg = load_config(tmp_path)
        assert cfg.disabled_rules == frozenset({"DTC01"})
        assert not cfg.rule_enabled("DTC01")
        assert cfg.rule_enabled("DTC02")

    def test_off_string_disables(self, tmp_path: Path) -> None:
        write_config(tmp_path, '[rules]\nDTC08 = "off"\n')
        assert load_config(tmp_path).disabled_rules == frozenset({"DTC08"})

    def test_true_is_a_noop(self, tmp_path: Path) -> None:
        write_config(tmp_path, "[rules]\nDTC01 = true\n")
        cfg = load_config(tmp_path)
        assert cfg.disabled_rules == frozenset()
        assert cfg.severity_overrides == {}

    def test_severity_string_overrides(self, tmp_path: Path) -> None:
        write_config(tmp_path, '[rules]\nDTR01 = "error"\nDTC01 = "advisory"\n')
        cfg = load_config(tmp_path)
        assert cfg.severity_overrides == {
            "DTR01": Severity.ERROR,
            "DTC01": Severity.ADVISORY,
        }
        assert cfg.severity_for("DTR01") == Severity.ERROR
        assert cfg.severity_for("DTC01") == Severity.ADVISORY

    def test_lowercase_codes_are_normalized(self, tmp_path: Path) -> None:
        write_config(tmp_path, '[rules]\ndtc01 = false\ndtr01 = "error"\n')
        cfg = load_config(tmp_path)
        assert cfg.disabled_rules == frozenset({"DTC01"})
        assert cfg.severity_overrides == {"DTR01": Severity.ERROR}

    def test_invalid_severity_raises(self, tmp_path: Path) -> None:
        write_config(tmp_path, '[rules]\nDTC01 = "loud"\n')
        with pytest.raises(ConfigError, match="severity must be one of"):
            load_config(tmp_path)

    def test_unknown_code_raises(self, tmp_path: Path) -> None:
        write_config(tmp_path, "[rules]\nDTZ99 = false\n")
        with pytest.raises(ConfigError, match="unknown rule 'DTZ99'"):
            load_config(tmp_path)

    def test_non_bool_non_string_value_raises(self, tmp_path: Path) -> None:
        write_config(tmp_path, "[rules]\nDTC01 = 3\n")
        with pytest.raises(ConfigError, match="expected false, true, or a severity string"):
            load_config(tmp_path)

    def test_rules_must_be_a_table(self, tmp_path: Path) -> None:
        write_config(tmp_path, 'rules = "strict"\n')
        with pytest.raises(ConfigError, match="'rules' must be a table"):
            load_config(tmp_path)


# ---------------------------------------------------------------------------
# ecosystems / ignore / misc scalars
# ---------------------------------------------------------------------------


class TestEcosystems:
    def test_list_accepted(self, tmp_path: Path) -> None:
        write_config(tmp_path, 'ecosystems = ["claude-code", "cursor"]\n')
        assert load_config(tmp_path).ecosystems == ("claude-code", "cursor")

    def test_non_list_raises(self, tmp_path: Path) -> None:
        write_config(tmp_path, 'ecosystems = "claude-code"\n')
        with pytest.raises(ConfigError, match="'ecosystems' must be a list of strings"):
            load_config(tmp_path)

    def test_non_string_items_raise(self, tmp_path: Path) -> None:
        write_config(tmp_path, "ecosystems = [1, 2]\n")
        with pytest.raises(ConfigError, match="'ecosystems' must be a list of strings"):
            load_config(tmp_path)


class TestIgnoreGlobs:
    def test_globs_accepted(self, tmp_path: Path) -> None:
        write_config(tmp_path, 'ignore = ["**/legacy.md", "vendor/**"]\n')
        assert load_config(tmp_path).ignore_globs == ("**/legacy.md", "vendor/**")

    def test_non_list_raises(self, tmp_path: Path) -> None:
        write_config(tmp_path, 'ignore = "**/legacy.md"\n')
        with pytest.raises(ConfigError, match="'ignore' must be a list of globs"):
            load_config(tmp_path)


class TestScalars:
    def test_misc_scalars(self, tmp_path: Path) -> None:
        write_config(
            tmp_path,
            "conflict_budget = 10\n"
            "include_soft = false\n"
            "max_pairs = 1000\n"
            "similarity_threshold = 0.5\n"
            "respect_gitignore = false\n",
        )
        cfg = load_config(tmp_path)
        assert cfg.conflict_budget == 10
        assert cfg.include_soft is False
        assert cfg.max_pairs == 1000
        assert cfg.similarity_threshold == 0.5
        assert cfg.respect_gitignore is False


# ---------------------------------------------------------------------------
# [jury]
# ---------------------------------------------------------------------------


class TestJuryTable:
    def test_model_and_max_pairs(self, tmp_path: Path) -> None:
        write_config(tmp_path, '[jury]\nmodel = "test-model"\nmax_pairs = 7\n')
        cfg = load_config(tmp_path)
        assert cfg.jury_model == "test-model"
        assert cfg.jury_max_pairs == 7

    def test_defaults_when_absent(self, tmp_path: Path) -> None:
        write_config(tmp_path, "")
        cfg = load_config(tmp_path)
        assert cfg.jury_model == "claude-haiku-4-5-20251001"
        assert cfg.jury_max_pairs == 200


# ---------------------------------------------------------------------------
# invalid TOML
# ---------------------------------------------------------------------------


class TestInvalidToml:
    def test_syntax_error_raises_config_error(self, tmp_path: Path) -> None:
        write_config(tmp_path, "this is not toml ===\n")
        with pytest.raises(ConfigError, match="invalid TOML"):
            load_config(tmp_path)
