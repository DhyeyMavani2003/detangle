"""Benchmark runner: ``python -m benchmarks.run_eval [--holdout] [--json out.json]``.

Two clearly separated measurements:

**Mutation suite (in-distribution).** For every (base tree x operator x seed)
triple this harness materializes the mutated tree into a temp directory, scans
it with detangle's deterministic lanes, and scores:

- **detection**: pair-granular where the operator injects a well-defined pair
  of conflicting texts (all nine operators record their two ``sites``): a run
  counts as DETECTED only when a single finding carries an expected code AND
  its evidence touches BOTH injected sites. Where a record carries no sites,
  scoring falls back to file-granular (any expected-code finding touching an
  injected file) and the report says so per operator;
- **false positives**: an equivalent-mutant control run counts as a false
  positive when any conflict-class code (DTC01-05, DTP01-04) fires — the base
  trees are conflict-clean, so any conflict finding is attributable to the
  injection;
- **clean baselines**: finding counts on the unmutated trees.

The mutators select and phrase injections through detangle's own parser and
lexicons (see the honesty caveat in :mod:`benchmarks.mutators`), so this
number is self-consistency, not generalization. Unique-injection counts are
reported alongside run counts because seeds mostly reshuffle target files,
not content.

**Holdout (novel phrasings).** The hand-authored, out-of-distribution cases in
:mod:`benchmarks.holdout` — realistic conflicts phrased without consulting
detangle's lexicons, plus benign-but-tricky trees. A conflict case counts as
detected only when a single finding carries an expected code and touches every
involved file; any conflict-class finding on a benign tree is a false
positive. This is the number that estimates real-world recall/precision.

It prints both tables (plus optional JSON) and always exits 0 — this is a
report on the linter's recall/precision, not a CI gate.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import time
from pathlib import Path

from detangle.config import Config
from detangle.pipeline import ScanResult, scan
from detangle.taxonomy import Severity

from .corpus import TREES
from .holdout import BENIGN_CASES, CONFLICT_CASES, HOLDOUT_FP_CODES
from .mutators import ALL_MUTATORS, CONFLICT_CODES, MutationError

DEFAULT_SEEDS = (0, 1, 2)


# ---------------------------------------------------------------------------
# Scan plumbing
# ---------------------------------------------------------------------------


def materialize(tree: dict[str, str], root: Path) -> None:
    """Write a ``{relpath: text}`` tree under ``root``."""
    for relpath, text in tree.items():
        p = root / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


def scan_tree(
    tree: dict[str, str],
    lanes: tuple[str, ...] = (),
    jury_max_pairs: int = 6,
    jury_model: str = "",
    screen_model: str = "",
    cache_dir: Path | None = None,
) -> ScanResult:
    """Materialize into a temp dir and run the pipeline.

    ``lanes`` may include "nli", "jury", and/or "screen" (screen implies
    jury). Jury calls are capped per tree by ``jury_max_pairs``; model
    overrides are backend-shaped strings ("sonnet", "opus", ...).
    """
    with tempfile.TemporaryDirectory(prefix="detangle-bench-") as td:
        root = Path(td)
        materialize(tree, root)
        cfg = Config(root=root)
        cfg.lane_nli = "nli" in lanes
        cfg.lane_jury = "jury" in lanes
        cfg.lane_screen = "screen" in lanes
        cfg.jury_max_pairs = jury_max_pairs
        if jury_model:
            cfg.jury_model = jury_model
        if screen_model:
            cfg.screen_model = screen_model
        # a persistent verdict/screen cache makes long LLM evals restartable
        # (uids are content+relpath addressed, so keys survive the temp dirs)
        cfg.cache_dir = cache_dir
        return scan(cfg)


def _touches(finding, files: set[str]) -> bool:
    return any(ev.span.path in files for ev in finding.evidence) or any(
        u.file.path in files for u in finding.units
    )


def detected(result: ScanResult, record: dict) -> bool:
    """File-granular detection: any expected-code finding touching an injected
    file. Kept as the fallback for records without well-defined sites."""
    expected = set(record["expected_codes"])
    injected = set(record["files"])
    return any(f.code in expected and _touches(f, injected) for f in result.findings)


# ---------------------------------------------------------------------------
# Pair-granular scoring
# ---------------------------------------------------------------------------


def _norm_text(s: str) -> str:
    """Normalize a quote/site text for containment comparison (strip markup
    punctuation the reporters add or drop: backticks, bullets, final period)."""
    s = s.replace("`", "").replace("*", "").replace('"', "").replace("'", "")
    s = re.sub(r"^[\s\-]+", "", s.strip())
    s = re.sub(r"\s+", " ", s)
    return s.lower().rstrip(".:;,!")


def _texts_match(a: str, b: str) -> bool:
    na, nb = _norm_text(a), _norm_text(b)
    if not na or not nb:
        return False
    if len(na) < 8 or len(nb) < 8:
        return na == nb
    return na in nb or nb in na


def _site_lines(tree: dict[str, str], path: str, text: str) -> set[int]:
    """1-based line numbers in ``tree[path]`` that contain the site text."""
    needle = text.strip()
    if not needle:
        return set()
    return {i + 1 for i, ln in enumerate(tree.get(path, "").split("\n")) if needle in ln}


def _touches_site(finding, site: dict, tree: dict[str, str]) -> bool:
    """Does this finding's evidence reach one injected site — same file, and
    either a span over the site's line(s) or a quote of the site's text?"""
    path, text = str(site["file"]), str(site["text"])
    lines = _site_lines(tree, path, text)
    probes = [(ev.span, ev.quote) for ev in finding.evidence]
    probes += [(u.span, u.text) for u in finding.units]
    for span, quoted in probes:
        if span.path != path:
            continue
        if lines and any(span.start_line <= n <= span.end_line for n in lines):
            return True
        if quoted and _texts_match(quoted, text):
            return True
    return False


def pair_detected(result: ScanResult, record: dict, tree: dict[str, str]) -> bool:
    """Pair-granular detection: a single finding must carry an expected code
    AND touch BOTH injected sites. Falls back to file-granular when the record
    carries fewer than two sites."""
    sites = record.get("sites") or []
    if len(sites) < 2:
        return detected(result, record)
    expected = set(record["expected_codes"])
    return any(
        f.code in expected and all(_touches_site(f, s, tree) for s in sites)
        for f in result.findings
    )


# ---------------------------------------------------------------------------
# Mutation-suite evaluation (in-distribution)
# ---------------------------------------------------------------------------


def evaluate(
    tree_names: list[str] | None = None,
    operator_names: list[str] | None = None,
    seeds: tuple[int, ...] = DEFAULT_SEEDS,
) -> dict:
    """Run the mutation suite; returns the JSON-serializable report."""
    trees = {n: TREES[n] for n in (tree_names or list(TREES))}
    mutators = {n: ALL_MUTATORS[n] for n in (operator_names or list(ALL_MUTATORS))}
    t0 = time.perf_counter()

    clean: dict[str, dict] = {}
    for name, tree in trees.items():
        ts = time.perf_counter()
        res = scan_tree(tree)
        by_code: dict[str, int] = {}
        for f in res.findings:
            by_code[f.code] = by_code.get(f.code, 0) + 1
        clean[name] = {
            "findings": len(res.findings),
            "errors": sum(1 for f in res.findings if f.severity >= Severity.ERROR),
            "conflict_codes": sum(1 for f in res.findings if f.code in CONFLICT_CODES),
            "by_code": by_code,
            "seconds": round(time.perf_counter() - ts, 3),
        }

    operators: dict[str, dict] = {}
    controls: dict[str, dict] = {}
    for op_name, mutate in mutators.items():
        runs = 0
        skipped = 0
        hits = 0
        file_hits = 0
        fps = 0
        per_code: dict[str, int] = {}
        conflict_seen: dict[str, int] = {}
        misses: list[dict] = []
        expected: list[str] = []
        unique_injections: set[str] = set()
        granularities: set[str] = set()
        is_control = False
        for tree_name, tree in trees.items():
            for seed in seeds:
                try:
                    mutated, record = mutate(tree, seed)
                except MutationError as e:
                    skipped += 1
                    misses.append({"tree": tree_name, "seed": seed, "skipped": str(e)})
                    continue
                runs += 1
                expected = list(record["expected_codes"])
                is_control = bool(record["control"])
                unique_injections.add(str(record["description"]))
                result = scan_tree(mutated)
                injected = set(record["files"])
                fired = sorted({f.code for f in result.findings if _touches(f, injected)})
                for code in fired:
                    if code in set(expected):
                        per_code[code] = per_code.get(code, 0) + 1
                conflict_codes = sorted(
                    {f.code for f in result.findings if f.code in CONFLICT_CODES}
                )
                for code in conflict_codes:
                    conflict_seen[code] = conflict_seen.get(code, 0) + 1
                if is_control:
                    if conflict_codes:
                        fps += 1
                    continue
                granularity = "pair" if len(record.get("sites") or []) >= 2 else "file"
                granularities.add(granularity)
                if detected(result, record):
                    file_hits += 1
                if pair_detected(result, record, mutated):
                    hits += 1
                else:
                    misses.append(
                        {
                            "tree": tree_name,
                            "seed": seed,
                            "codes_seen": fired,
                            "granularity": granularity,
                        }
                    )
        entry = {
            "runs": runs,
            "skipped": skipped,
            "expected_codes": expected,
            "per_code": per_code,
            "unique_injections": len(unique_injections),
        }
        if is_control:
            entry.update(
                {
                    "false_positives": fps,
                    "fp_rate": round(fps / runs, 4) if runs else 0.0,
                    "conflict_codes_seen": conflict_seen,
                }
            )
            controls[op_name] = entry
        else:
            entry.update(
                {
                    # pair-granular where sites are defined, else file-granular
                    "granularity": "pair" if granularities == {"pair"} else "file",
                    "detected": hits,
                    "rate": round(hits / runs, 4) if runs else 0.0,
                    "detected_file_granular": file_hits,
                    "misses": misses,
                }
            )
            operators[op_name] = entry

    op_runs = sum(o["runs"] for o in operators.values())
    op_hits = sum(o["detected"] for o in operators.values())
    op_file_hits = sum(o["detected_file_granular"] for o in operators.values())
    ctl_runs = sum(c["runs"] for c in controls.values())
    ctl_fps = sum(c["false_positives"] for c in controls.values())
    return {
        "tool": "detangle-benchmark",
        "suite": "mutation suite (in-distribution)",
        "seeds": list(seeds),
        "trees": list(trees),
        "clean": clean,
        "operators": operators,
        "controls": controls,
        "totals": {
            "operator_runs": op_runs,
            "unique_injections": sum(o["unique_injections"] for o in operators.values()),
            "detected": op_hits,
            "detection_rate": round(op_hits / op_runs, 4) if op_runs else 0.0,
            "detected_file_granular": op_file_hits,
            "detection_rate_file_granular": round(op_file_hits / op_runs, 4) if op_runs else 0.0,
            "control_runs": ctl_runs,
            "control_false_positives": ctl_fps,
            "control_fp_rate": round(ctl_fps / ctl_runs, 4) if ctl_runs else 0.0,
            "wall_clock_s": round(time.perf_counter() - t0, 3),
        },
    }


# ---------------------------------------------------------------------------
# Holdout evaluation (novel phrasings)
# ---------------------------------------------------------------------------


def _touches_file(finding, path: str) -> bool:
    return any(ev.span.path == path for ev in finding.evidence) or any(
        u.file.path == path for u in finding.units
    )


def holdout_detected(result: ScanResult, case: dict) -> bool:
    """A holdout conflict counts as detected only when a single finding carries
    an expected code AND touches every involved file."""
    expected = set(case["expected_codes"])
    involved = list(case["involved_files"])
    return any(
        f.code in expected and all(_touches_file(f, p) for p in involved) for f in result.findings
    )


# the LLM jury maps verdicts through its own (coarser) code vocabulary, so a
# numeric clash it judges may surface as DTC01/DTC02 where the case author
# expected DTC03. Class-lenient scoring credits ANY conflict-class code on the
# right evidence — reported alongside strict, never instead of it.
_LENIENT_CONFLICT_CODES = frozenset(
    {"DTC01", "DTC02", "DTC03", "DTC04", "DTC05", "DTC08", "DTP02", "DTP03", "DTP04"}
)


def holdout_detected_lenient(result: ScanResult, case: dict) -> bool:
    """Class-lenient detection: for conflict-class cases, any conflict-class
    finding touching every involved file counts. DTR/DTS cases stay strict."""
    if holdout_detected(result, case):
        return True
    primary = list(case["expected_codes"])[0]
    if primary[:3] not in ("DTC", "DTP"):
        return False
    involved = list(case["involved_files"])
    return any(
        f.code in _LENIENT_CONFLICT_CODES and all(_touches_file(f, p) for p in involved)
        for f in result.findings
    )


def evaluate_holdout(
    case_ids: list[str] | None = None,
    lanes: tuple[str, ...] = (),
    jury_model: str = "",
    screen_model: str = "",
    cache_dir: Path | None = None,
) -> dict:
    """Run the hand-authored holdout set; returns the JSON-serializable report.

    Pass ``lanes=("nli", "jury")`` (or ``("nli", "screen")`` for the full
    cascade) to measure the hybrid pipelines instead of the deterministic
    lane alone; model overrides are backend-shaped ("sonnet", "opus", ...).
    """
    t0 = time.perf_counter()
    wanted = set(case_ids) if case_ids else None

    conflict_results: list[dict] = []
    per_code: dict[str, dict[str, int]] = {}
    for case in CONFLICT_CASES:
        if wanted is not None and case["id"] not in wanted:
            continue
        result = scan_tree(  # type: ignore[arg-type]
            dict(case["tree"]),
            lanes=lanes,
            jury_model=jury_model,
            screen_model=screen_model,
            cache_dir=cache_dir,
        )
        hit = holdout_detected(result, case)
        lenient_hit = holdout_detected_lenient(result, case)
        primary = list(case["expected_codes"])[0]
        stats = per_code.setdefault(primary, {"cases": 0, "detected": 0, "lenient": 0})
        stats["cases"] += 1
        stats["detected"] += int(hit)
        stats["lenient"] += int(lenient_hit)
        conflict_results.append(
            {
                "id": case["id"],
                "detected": hit,
                "detected_lenient": lenient_hit,
                "expected_codes": list(case["expected_codes"]),
                "codes_seen": sorted({f.code for f in result.findings}),
                "description": case["description"],
            }
        )

    benign_results: list[dict] = []
    for case in BENIGN_CASES:
        if wanted is not None and case["id"] not in wanted:
            continue
        result = scan_tree(  # type: ignore[arg-type]
            dict(case["tree"]),
            lanes=lanes,
            jury_model=jury_model,
            screen_model=screen_model,
            cache_dir=cache_dir,
        )
        fp_codes = sorted({f.code for f in result.findings if f.code in HOLDOUT_FP_CODES})
        benign_results.append(
            {
                "id": case["id"],
                "false_positive": bool(fp_codes),
                "conflict_codes_seen": fp_codes,
                "description": case["description"],
            }
        )

    n_conflicts = len(conflict_results)
    n_detected = sum(1 for c in conflict_results if c["detected"])
    n_lenient = sum(1 for c in conflict_results if c.get("detected_lenient"))
    n_benign = len(benign_results)
    n_fps = sum(1 for b in benign_results if b["false_positive"])
    return {
        "tool": "detangle-benchmark",
        "suite": "holdout (novel phrasings)",
        "scoring": (
            "a conflict case counts as detected only when one finding carries an "
            "expected code and its evidence touches every involved file; any "
            "conflict-class code on a benign tree is a false positive"
        ),
        "conflicts": conflict_results,
        "benign": benign_results,
        "per_code": {c: per_code[c] for c in sorted(per_code)},
        "totals": {
            "conflict_cases": n_conflicts,
            "detected": n_detected,
            "recall": round(n_detected / n_conflicts, 4) if n_conflicts else 0.0,
            "detected_lenient": n_lenient,
            "recall_lenient": round(n_lenient / n_conflicts, 4) if n_conflicts else 0.0,
            "benign_cases": n_benign,
            "false_positives": n_fps,
            "fp_rate": round(n_fps / n_benign, 4) if n_benign else 0.0,
            "wall_clock_s": round(time.perf_counter() - t0, 3),
        },
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _fmt_codes(counts: dict[str, int]) -> str:
    return ", ".join(f"{c}x{n}" for c, n in sorted(counts.items())) or "-"


def render_table(report: dict) -> str:
    lines: list[str] = []
    t = report["totals"]
    lines.append("== mutation suite (in-distribution) ==")
    lines.append("injections are selected and phrased via detangle's own parser/lexicons;")
    lines.append("this measures self-consistency, NOT generalization — see the holdout table.")
    lines.append(
        f"trees: {len(report['trees'])}   seeds: {report['seeds']}   "
        f"wall clock: {t['wall_clock_s']}s"
    )
    lines.append("")
    lines.append(f"{'clean tree':<28} {'findings':>8} {'errors':>7} {'conflicts':>9} {'sec':>6}")
    for name, c in report["clean"].items():
        lines.append(
            f"  {name:<26} {c['findings']:>8} {c['errors']:>7} "
            f"{c['conflict_codes']:>9} {c['seconds']:>6}"
        )
    lines.append("")
    lines.append(
        f"{'operator':<28} {'runs':>5} {'uniq':>5} {'det':>4} {'rate':>6} {'gran':>5}  "
        f"{'detected codes'}"
    )
    for name, o in report["operators"].items():
        lines.append(
            f"  {name:<26} {o['runs']:>5} {o['unique_injections']:>5} {o['detected']:>4} "
            f"{o['rate']:>6.0%} {o['granularity']:>5}  {_fmt_codes(o['per_code'])}"
        )
    lines.append("  (uniq = distinct injections across runs; gran=pair requires one finding's")
    lines.append("   evidence to touch BOTH injected sites, gran=file is the lenient fallback)")
    lines.append("")
    lines.append(f"{'control':<28} {'runs':>5} {'FPs':>4} {'rate':>6}  {'conflict codes seen'}")
    for name, c in report["controls"].items():
        lines.append(
            f"  {name:<26} {c['runs']:>5} {c['false_positives']:>4} {c['fp_rate']:>6.0%}  "
            f"{_fmt_codes(c['conflict_codes_seen'])}"
        )
    lines.append("")
    lines.append(
        f"in-distribution detection (pair-granular): {t['detected']}/{t['operator_runs']} "
        f"({t['detection_rate']:.1%}, {t['unique_injections']} unique injections; "
        f"file-granular would be {t['detected_file_granular']}/{t['operator_runs']})   "
        f"control FP rate: {t['control_false_positives']}/{t['control_runs']} "
        f"({t['control_fp_rate']:.1%})"
    )
    return "\n".join(lines)


def render_holdout_table(report: dict) -> str:
    lines: list[str] = []
    t = report["totals"]
    lines.append("== holdout (novel phrasings) ==")
    lines.append("hand-authored, out-of-distribution cases; phrasings were NOT drawn from")
    lines.append("detangle's lexicons. This estimates real-world recall/precision.")
    lines.append(f"wall clock: {t['wall_clock_s']}s")
    lines.append("")
    lines.append(f"{'conflict class':<16} {'cases':>5} {'det':>4} {'recall':>7}")
    for code, stats in report["per_code"].items():
        rate = stats["detected"] / stats["cases"] if stats["cases"] else 0.0
        lines.append(f"  {code:<14} {stats['cases']:>5} {stats['detected']:>4} {rate:>7.0%}")
    lines.append("")
    missed = [c for c in report["conflicts"] if not c["detected"]]
    if missed:
        lines.append("missed conflict cases:")
        for c in missed:
            seen = ", ".join(c["codes_seen"]) or "no findings"
            lines.append(f"  {c['id']:<34} expected {'/'.join(c['expected_codes'])}; saw: {seen}")
        lines.append("")
    fps = [b for b in report["benign"] if b["false_positive"]]
    if fps:
        lines.append("benign trees with conflict-class false positives:")
        for b in fps:
            lines.append(f"  {b['id']:<34} fired: {', '.join(b['conflict_codes_seen'])}")
        lines.append("")
    lines.append(
        f"holdout recall: {t['detected']}/{t['conflict_cases']} ({t['recall']:.1%}) strict, "
        f"{t.get('detected_lenient', t['detected'])}/{t['conflict_cases']} "
        f"({t.get('recall_lenient', t['recall']):.1%}) class-lenient   "
        f"holdout FP rate: {t['false_positives']}/{t['benign_cases']} ({t['fp_rate']:.1%})"
    )
    lines.append(f"scoring: {report['scoring']}")
    lines.append(
        "class-lenient credits any conflict-class code on the right evidence — the jury "
        "labels through its own verdict vocabulary, so e.g. a numeric clash may surface "
        "as DTC01/DTC02"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m benchmarks.run_eval",
        description=(
            "Run the seeded-conflict mutation suite (in-distribution) and the "
            "hand-authored holdout set (novel phrasings)."
        ),
    )
    p.add_argument("--json", type=Path, default=None, help="also write the full report as JSON")
    p.add_argument(
        "--holdout",
        action="store_true",
        help="run only the hand-authored holdout set (it is part of the default run too)",
    )
    p.add_argument(
        "--lanes",
        default="",
        help='comma-separated optional lanes for the HOLDOUT scans, e.g. "nli,jury" '
        "(the mutation suite always runs deterministic-only; jury needs a backend)",
    )
    p.add_argument("--jury-model", default="", help="jury model override (backend-shaped)")
    p.add_argument("--screen-model", default="", help="screen model override (backend-shaped)")
    p.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="persistent verdict/screen cache directory for the LLM lanes "
        "(makes long holdout evals restartable and repeat runs cheap)",
    )
    p.add_argument("--cases", default=None, help="comma-separated holdout case ids (default: all)")
    p.add_argument("--trees", default=None, help="comma-separated tree names (default: all)")
    p.add_argument(
        "--operators", default=None, help="comma-separated operator/control names (default: all)"
    )
    p.add_argument("--seeds", default=None, help="comma-separated integer seeds (default: 0,1,2)")
    args = p.parse_args(argv)

    tree_names = [s.strip() for s in args.trees.split(",")] if args.trees else None
    op_names = [s.strip() for s in args.operators.split(",")] if args.operators else None
    seeds = tuple(int(s) for s in args.seeds.split(",")) if args.seeds else DEFAULT_SEEDS
    for name in tree_names or ():
        if name not in TREES:
            print(f"error: unknown tree {name!r} (have: {', '.join(TREES)})", file=sys.stderr)
            return 0  # a report, not a gate — but nothing to report
    for name in op_names or ():
        if name not in ALL_MUTATORS:
            print(
                f"error: unknown operator {name!r} (have: {', '.join(ALL_MUTATORS)})",
                file=sys.stderr,
            )
            return 0

    combined: dict[str, dict] = {}
    if not args.holdout:
        mutation_report = evaluate(tree_names, op_names, seeds)
        combined["mutation_suite"] = mutation_report
        print(render_table(mutation_report))
        print()
    lanes = tuple(x.strip() for x in args.lanes.split(",") if x.strip())
    case_ids = [x.strip() for x in args.cases.split(",")] if args.cases else None
    holdout_report = evaluate_holdout(
        case_ids=case_ids,
        lanes=lanes,
        jury_model=args.jury_model,
        screen_model=args.screen_model,
        cache_dir=args.cache_dir,
    )
    combined["holdout"] = holdout_report
    print(render_holdout_table(holdout_report))
    if args.json:
        args.json.write_text(json.dumps(combined, indent=2) + "\n", encoding="utf-8")
        print(f"\nwrote {args.json}")
    return 0  # always: this is a measurement, not a gate


if __name__ == "__main__":
    raise SystemExit(main())
