"""Tests for the seeded-conflict benchmark harness (benchmarks/)."""

from __future__ import annotations

from pathlib import Path

import pytest
from benchmarks.corpus import TREES
from benchmarks.mutators import ALL_MUTATORS, CONFLICT_CODES, CONTROLS, OPERATORS
from benchmarks.run_eval import detected, evaluate, render_table

from detangle.config import Config
from detangle.pipeline import ScanResult, scan
from detangle.taxonomy import RULES, Severity

from .conftest import write_tree


def _scan_tree(tmp_path: Path, tree: dict[str, str], subdir: str) -> ScanResult:
    root = tmp_path / subdir
    root.mkdir(parents=True, exist_ok=True)
    write_tree(root, tree)
    return scan(Config(root=root))


# ---------------------------------------------------------------------------
# Clean corpus trees
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(TREES))
def test_clean_tree_has_no_error_findings(tmp_path: Path, name: str) -> None:
    result = _scan_tree(tmp_path, TREES[name], name)
    errors = [f for f in result.findings if f.severity >= Severity.ERROR]
    assert not errors, f"{name}: {[(f.code, f.message) for f in errors]}"


@pytest.mark.parametrize("name", sorted(TREES))
def test_clean_tree_has_no_conflict_class_findings(tmp_path: Path, name: str) -> None:
    """Base trees must be conflict-free so every conflict finding on a mutated
    tree is attributable to the injection."""
    result = _scan_tree(tmp_path, TREES[name], name)
    conflicts = [f for f in result.findings if f.code in CONFLICT_CODES]
    assert not conflicts, f"{name}: {[(f.code, f.message) for f in conflicts]}"


@pytest.mark.parametrize("name", sorted(TREES))
def test_clean_tree_parses_into_units(tmp_path: Path, name: str) -> None:
    result = _scan_tree(tmp_path, TREES[name], name)
    assert len(result.corpus.files) >= 3
    assert len(result.units) >= 5


def test_corpus_has_expected_shapes() -> None:
    assert set(TREES) == {"claude-webapp", "agents-monorepo", "cursor-spa", "mixed-stack"}
    assert ".claude/skills/changelog/SKILL.md" in TREES["claude-webapp"]
    assert "services/api/AGENTS.md" in TREES["agents-monorepo"]
    assert sum(1 for p in TREES["cursor-spa"] if p.endswith(".mdc")) == 3


# ---------------------------------------------------------------------------
# Mutators
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("op_name", sorted(ALL_MUTATORS))
@pytest.mark.parametrize("tree_name", sorted(TREES))
def test_mutator_contract(tree_name: str, op_name: str) -> None:
    base = TREES[tree_name]
    before = dict(base)
    mutated, record = ALL_MUTATORS[op_name](base, seed=0)

    assert base == before, "mutator must not modify the input tree"
    assert mutated != base, "mutated tree must differ from the base"

    assert record["operator"] == op_name
    assert record["expected_codes"], "record must carry non-empty expected_codes"
    assert all(code in RULES for code in record["expected_codes"])
    assert record["files"], "record must name the injected files"
    assert all(f in mutated for f in record["files"])
    assert record["control"] == (op_name in CONTROLS)
    # something in every named file was actually created or changed... except
    # the disagreement *source* file, which may be untouched: at least one
    # named file must be new or edited.
    changed = [f for f in record["files"] if base.get(f) != mutated[f]]
    assert changed, "injection must create or edit at least one recorded file"


@pytest.mark.parametrize("op_name", sorted(ALL_MUTATORS))
def test_mutator_is_deterministic_per_seed(op_name: str) -> None:
    base = TREES["claude-webapp"]
    first = ALL_MUTATORS[op_name](base, seed=7)
    second = ALL_MUTATORS[op_name](base, seed=7)
    assert first == second


def test_conflict_operators_expect_only_taxonomy_codes() -> None:
    assert set(OPERATORS) == {
        "deontic_flip",
        "parameter_clash",
        "scope_overlap_clash",
        "conditional_contradiction",
        "terminology_drift",
        "cross_layer_clash",
        "duplicate_injection",
        "format_clash",
        "trigger_overlap",
    }
    assert set(CONTROLS) == {"paraphrase", "benign_specialization"}


# ---------------------------------------------------------------------------
# End-to-end scoring (small subset: 2 operators x 1 seed)
# ---------------------------------------------------------------------------


def test_eval_subset_detects_seeded_conflicts() -> None:
    report = evaluate(
        tree_names=["claude-webapp"],
        operator_names=["deontic_flip", "parameter_clash"],
        seeds=(0,),
    )
    for op in ("deontic_flip", "parameter_clash"):
        assert report["operators"][op]["runs"] == 1
        assert report["operators"][op]["detected"] > 0, report["operators"][op]
    assert report["totals"]["detection_rate"] > 0
    assert report["clean"]["claude-webapp"]["errors"] == 0
    assert report["totals"]["wall_clock_s"] >= 0
    # the table renders without blowing up and mentions both operators
    table = render_table(report)
    assert "deontic_flip" in table and "parameter_clash" in table


def test_paraphrase_control_fires_no_conflict_codes(tmp_path: Path) -> None:
    base = TREES["mixed-stack"]
    mutated, record = ALL_MUTATORS["paraphrase"](base, seed=0)
    result = _scan_tree(tmp_path, mutated, "paraphrased")
    conflict = [f for f in result.findings if f.code in CONFLICT_CODES]
    assert not conflict, [(f.code, f.message) for f in conflict]
    # redundancy findings are allowed (a paraphrase IS a redundancy) — but the
    # scoring helper must not count them as a detection of a conflict either
    assert record["control"] is True


def test_detected_requires_expected_code_touching_injected_file(tmp_path: Path) -> None:
    mutated, record = ALL_MUTATORS["deontic_flip"](TREES["agents-monorepo"], seed=1)
    result = _scan_tree(tmp_path, mutated, "flipped")
    assert detected(result, record)
    # a record pointing at untouched files must not count as detected
    bogus = dict(record, files=["no/such/file.md"])
    assert not detected(result, bogus)


def test_control_fp_evaluation_reports_zero_on_subset() -> None:
    report = evaluate(
        tree_names=["cursor-spa"],
        operator_names=["paraphrase", "benign_specialization"],
        seeds=(0,),
    )
    assert report["totals"]["control_runs"] == 2
    assert report["totals"]["control_false_positives"] == 0
