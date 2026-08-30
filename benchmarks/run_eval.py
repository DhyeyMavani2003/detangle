"""Seeded-conflict benchmark runner: ``python -m benchmarks.run_eval [--json out.json]``.

For every (base tree x operator x seed) triple this harness materializes the
mutated tree into a temp directory, scans it with detangle's deterministic
lanes, and scores:

- **detection**: a seeded conflict counts as DETECTED when any finding's code
  is in the injection record's ``expected_codes`` AND its evidence touches an
  injected file;
- **false positives**: an equivalent-mutant control run counts as a false
  positive when any conflict-class code (DTC01-05, DTP01-04) fires — the base
  trees are conflict-clean, so any conflict finding is attributable to the
  injection;
- **clean baselines**: finding counts on the unmutated trees.

It prints a compact table (plus optional JSON) and always exits 0 — this is a
report on the linter's recall/precision, not a CI gate.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

from detangle.config import Config
from detangle.pipeline import ScanResult, scan
from detangle.taxonomy import Severity

from .corpus import TREES
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


def scan_tree(tree: dict[str, str]) -> ScanResult:
    """Materialize into a temp dir and run the full deterministic pipeline."""
    with tempfile.TemporaryDirectory(prefix="detangle-bench-") as td:
        root = Path(td)
        materialize(tree, root)
        return scan(Config(root=root))


def _touches(finding, files: set[str]) -> bool:
    return any(ev.span.path in files for ev in finding.evidence) or any(
        u.file.path in files for u in finding.units
    )


def detected(result: ScanResult, record: dict) -> bool:
    """Did the scan catch the seeded defect this record describes?"""
    expected = set(record["expected_codes"])
    injected = set(record["files"])
    return any(f.code in expected and _touches(f, injected) for f in result.findings)


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def evaluate(
    tree_names: list[str] | None = None,
    operator_names: list[str] | None = None,
    seeds: tuple[int, ...] = DEFAULT_SEEDS,
) -> dict:
    """Run the benchmark; returns the JSON-serializable report."""
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
        fps = 0
        per_code: dict[str, int] = {}
        conflict_seen: dict[str, int] = {}
        misses: list[dict] = []
        expected: list[str] = []
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
                elif detected(result, record):
                    hits += 1
                else:
                    misses.append({"tree": tree_name, "seed": seed, "codes_seen": fired})
        entry = {
            "runs": runs,
            "skipped": skipped,
            "expected_codes": expected,
            "per_code": per_code,
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
                    "detected": hits,
                    "rate": round(hits / runs, 4) if runs else 0.0,
                    "misses": misses,
                }
            )
            operators[op_name] = entry

    op_runs = sum(o["runs"] for o in operators.values())
    op_hits = sum(o["detected"] for o in operators.values())
    ctl_runs = sum(c["runs"] for c in controls.values())
    ctl_fps = sum(c["false_positives"] for c in controls.values())
    return {
        "tool": "detangle-benchmark",
        "seeds": list(seeds),
        "trees": list(trees),
        "clean": clean,
        "operators": operators,
        "controls": controls,
        "totals": {
            "operator_runs": op_runs,
            "detected": op_hits,
            "detection_rate": round(op_hits / op_runs, 4) if op_runs else 0.0,
            "control_runs": ctl_runs,
            "control_false_positives": ctl_fps,
            "control_fp_rate": round(ctl_fps / ctl_runs, 4) if ctl_runs else 0.0,
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
    lines.append("detangle seeded-conflict benchmark")
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
    lines.append(f"{'operator':<28} {'runs':>5} {'det':>4} {'rate':>6}  {'detected codes'}")
    for name, o in report["operators"].items():
        lines.append(
            f"  {name:<26} {o['runs']:>5} {o['detected']:>4} {o['rate']:>6.0%}  "
            f"{_fmt_codes(o['per_code'])}"
        )
    lines.append("")
    lines.append(f"{'control':<28} {'runs':>5} {'FPs':>4} {'rate':>6}  {'conflict codes seen'}")
    for name, c in report["controls"].items():
        lines.append(
            f"  {name:<26} {c['runs']:>5} {c['false_positives']:>4} {c['fp_rate']:>6.0%}  "
            f"{_fmt_codes(c['conflict_codes_seen'])}"
        )
    lines.append("")
    lines.append(
        f"detection: {t['detected']}/{t['operator_runs']} ({t['detection_rate']:.1%})   "
        f"control FP rate: {t['control_false_positives']}/{t['control_runs']} "
        f"({t['control_fp_rate']:.1%})"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m benchmarks.run_eval",
        description="Run the seeded-conflict benchmark over the clean corpus trees.",
    )
    p.add_argument("--json", type=Path, default=None, help="also write the full report as JSON")
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

    report = evaluate(tree_names, op_names, seeds)
    print(render_table(report))
    if args.json:
        args.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"\nwrote {args.json}")
    return 0  # always: this is a measurement, not a gate


if __name__ == "__main__":
    raise SystemExit(main())
