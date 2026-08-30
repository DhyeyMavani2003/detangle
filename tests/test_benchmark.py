"""Tests for the seeded-conflict benchmark harness (benchmarks/)."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest
from benchmarks.corpus import TREES
from benchmarks.holdout import (
    BENIGN_CASES,
    CONFLICT_CASES,
    HOLDOUT_CLAIMED_CODES,
    HOLDOUT_FP_CODES,
)
from benchmarks.mutators import ALL_MUTATORS, CONFLICT_CODES, CONTROLS, OPERATORS
from benchmarks.run_eval import (
    detected,
    evaluate,
    evaluate_holdout,
    holdout_detected,
    pair_detected,
    render_holdout_table,
    render_table,
)

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


# ---------------------------------------------------------------------------
# Pair-granular scoring (review findings #39/#43: any-code/any-file scoring
# could credit a wrong-pair finding; detection must cover both injected sites)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("op_name", sorted(OPERATORS))
def test_operator_records_carry_two_locatable_sites(op_name: str) -> None:
    mutated, record = OPERATORS[op_name](TREES["claude-webapp"], seed=0)
    sites = record.get("sites")
    assert sites and len(sites) == 2, "every operator must record both conflict sites"
    for site in sites:
        assert site["file"] in record["files"]
        assert site["file"] in mutated
        assert str(site["text"]).strip(), "site text must be non-empty"
        assert str(site["text"]).strip() in mutated[str(site["file"])], (
            "site text must be locatable in the mutated tree"
        )


def test_pair_detected_requires_evidence_on_both_sites(tmp_path: Path) -> None:
    mutated, record = ALL_MUTATORS["deontic_flip"](TREES["claude-webapp"], seed=0)
    result = _scan_tree(tmp_path, mutated, "pairflip")
    assert pair_detected(result, record, mutated)
    # a record whose second site carries text no finding quotes (and which sits
    # on no evidenced line) must NOT count — this is the wrong-pair guard
    bogus = dict(record)
    bogus["sites"] = [
        record["sites"][0],
        {
            "file": record["sites"][1]["file"],
            "text": "an utterly unrelated sentence that appears in no finding",
        },
    ]
    assert not pair_detected(result, bogus, mutated)
    # without sites the scorer falls back to file-granular semantics
    fallback = {k: v for k, v in record.items() if k != "sites"}
    assert pair_detected(result, fallback, mutated) == detected(result, fallback)


def test_evaluate_reports_pair_granularity_and_unique_injections() -> None:
    report = evaluate(tree_names=["claude-webapp"], operator_names=["deontic_flip"], seeds=(0,))
    entry = report["operators"]["deontic_flip"]
    assert entry["granularity"] == "pair"
    assert entry["unique_injections"] >= 1
    assert "detected_file_granular" in entry
    assert "unique_injections" in report["totals"]
    table = render_table(report)
    assert "in-distribution" in table, "the mutation suite must be labeled as such"


# ---------------------------------------------------------------------------
# Holdout set (review findings #39/#40/#41: out-of-distribution measurement)
# ---------------------------------------------------------------------------


def test_holdout_conflict_cases_are_structurally_valid() -> None:
    assert len(CONFLICT_CASES) >= 24
    ids = [c["id"] for c in CONFLICT_CASES]
    assert len(ids) == len(set(ids)), "case ids must be unique"
    for case in CONFLICT_CASES:
        assert case["description"], case["id"]
        assert case["expected_codes"], case["id"]
        assert all(code in RULES for code in case["expected_codes"]), case["id"]
        assert case["involved_files"], case["id"]
        assert all(f in case["tree"] for f in case["involved_files"]), case["id"]
        assert all(isinstance(t, str) and t for t in case["tree"].values()), case["id"]


def test_holdout_covers_every_claimed_conflict_class() -> None:
    counts = Counter(c["expected_codes"][0] for c in CONFLICT_CASES)
    for code in HOLDOUT_CLAIMED_CODES:
        assert counts[code] >= 2, f"{code}: need at least two holdout cases, have {counts[code]}"


def test_holdout_benign_cases_carry_no_conflict_expectations() -> None:
    assert len(BENIGN_CASES) >= 16
    ids = [c["id"] for c in BENIGN_CASES]
    assert len(ids) == len(set(ids)), "case ids must be unique"
    for case in BENIGN_CASES:
        assert case["description"], case["id"]
        assert case["tree"], case["id"]
        # benign trees expect nothing; any conflict-class finding is an FP
        assert not case.get("expected_codes"), case["id"]


def test_holdout_fp_codes_span_conflict_classes_and_dtx02() -> None:
    # DTP03 is deliberately excluded: it flags INTENTIONAL carve-outs as
    # fragile at advisory severity — designed behavior on benign trees.
    assert CONFLICT_CODES - {"DTP03"} <= HOLDOUT_FP_CODES
    assert "DTP03" not in HOLDOUT_FP_CODES
    assert "DTX02" in HOLDOUT_FP_CODES
    assert all(code in RULES for code in HOLDOUT_FP_CODES)


def test_holdout_eval_runs_and_reports_shape() -> None:
    subset = [str(CONFLICT_CASES[0]["id"]), str(BENIGN_CASES[0]["id"])]
    report = evaluate_holdout(case_ids=subset)
    assert report["suite"] == "holdout (novel phrasings)"
    assert report["totals"]["conflict_cases"] == 1
    assert report["totals"]["benign_cases"] == 1
    assert 0.0 <= report["totals"]["recall"] <= 1.0
    assert 0.0 <= report["totals"]["fp_rate"] <= 1.0
    assert report["per_code"], "per-code recall must be reported"
    table = render_holdout_table(report)
    assert "holdout (novel phrasings)" in table
    assert "recall" in table


def test_holdout_detected_requires_touching_every_involved_file(tmp_path: Path) -> None:
    # a case detangle demonstrably catches, to exercise the strict criterion
    case = next(c for c in CONFLICT_CASES if c["id"] == "dtr01-cross-file-duplicate")
    result = _scan_tree(tmp_path, dict(case["tree"]), "holdout-dup")
    assert holdout_detected(result, case)
    bogus = dict(case, involved_files=list(case["involved_files"]) + ["no/such/file.md"])
    assert not holdout_detected(result, bogus)
