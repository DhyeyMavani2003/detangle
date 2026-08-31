"""The detangle command-line interface.

detangle scan [path]           full scan
detangle diff [path] [--base]  findings introduced by changed config files
detangle explain DTC01         rule documentation
detangle rules                 list all rules
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

from . import __version__
from .config import ConfigError, load_config
from .pipeline import ScanResult, scan
from .taxonomy import RULES, Severity

# bare `--baseline` must mean "the configured default", not clobber a path
# set in [detangle.baseline] — so the const is a sentinel, resolved after
# the config file is loaded
_BASELINE_DEFAULT_SENTINEL = "\0default"


def _plain(text: str) -> str:
    """Strip terminal control characters from baseline-sourced text before
    printing — the artifact is hand-editable, so its strings are untrusted."""
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", text)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="detangle",
        description=(
            "Merge-conflict detection for English-as-code: finds conflicting, "
            "contradictory, redundant, shadowed, and precedence-ambiguous "
            "instructions across your agent configuration."
        ),
    )
    p.add_argument("--version", action="version", version=f"detangle {__version__}")
    sub = p.add_subparsers(dest="command")

    def add_scan_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("path", nargs="?", default=".", help="repository root (default: .)")
        sp.add_argument("--config", type=Path, default=None, help="path to .detangle.toml")
        sp.add_argument(
            "--format",
            choices=("console", "json", "sarif", "markdown"),
            default="console",
            dest="fmt",
        )
        sp.add_argument("--output", "-o", type=Path, default=None, help="write report to file")
        sp.add_argument(
            "--fail-on",
            choices=tuple(s.label for s in Severity),
            default=None,
            help="exit non-zero at or above this severity (default: error)",
        )
        sp.add_argument("--nli", action="store_true", help="enable the NLI lane")
        sp.add_argument("--jury", action="store_true", help="enable the LLM jury lane")
        sp.add_argument(
            "--screen",
            action="store_true",
            help="enable the whole-config LLM screen sweep (implies --jury; strongest model)",
        )
        sp.add_argument(
            "--deep",
            action="store_true",
            help="thoroughness-first pass: every available lane, per-class screen "
            "sweeps, jury cap lifted — built for overnight CI (hours are fine)",
        )
        sp.add_argument(
            "--baseline",
            nargs="?",
            const=_BASELINE_DEFAULT_SENTINEL,
            default=None,
            metavar="FILE",
            help="triage baseline carrying human verdicts across runs "
            "(default file when given without a value: .detangle-baseline.json, "
            "or the [detangle.baseline] path from the config file)",
        )
        sp.add_argument(
            "--update-baseline",
            action="store_true",
            help="write the merged baseline back after the scan (implies --baseline)",
        )
        sp.add_argument(
            "--only-new",
            action="store_true",
            help="report only findings that are new (or regressed) vs the baseline",
        )
        sp.add_argument(
            "--fail-on-new",
            action="store_true",
            help="exit non-zero only for new/regression findings at/above --fail-on",
        )
        sp.add_argument("--no-soft", action="store_true", help="hide advisory/info findings")
        sp.add_argument("-v", "--verbose", action="store_true")
        sp.add_argument(
            "--select",
            default=None,
            help="comma-separated rule codes to run exclusively (e.g. DTC01,DTC03)",
        )

    sp_scan = sub.add_parser("scan", help="scan a repository")
    add_scan_args(sp_scan)

    sp_diff = sub.add_parser(
        "diff", help="report only findings that involve config files changed vs --base"
    )
    add_scan_args(sp_diff)
    sp_diff.add_argument(
        "--base", default="origin/main", help="git ref to diff against (default: origin/main)"
    )

    sp_explain = sub.add_parser("explain", help="explain a rule code")
    sp_explain.add_argument("code", help="rule code, e.g. DTC01 (a fingerprint's prefix works)")

    sub.add_parser("rules", help="list all rules")

    sp_bl = sub.add_parser(
        "baseline", help="triage the findings baseline (answer, approve, override)"
    )
    bl_sub = sp_bl.add_subparsers(dest="baseline_command", required=True)

    def add_baseline_args(bp: argparse.ArgumentParser) -> None:
        bp.add_argument("path", nargs="?", default=".", help="scanned root (default: .)")
        bp.add_argument(
            "--baseline",
            default=".detangle-baseline.json",
            metavar="FILE",
            help="baseline file, relative to the scanned root",
        )

    bl_list = bl_sub.add_parser("list", help="list baseline entries (the triage queue)")
    add_baseline_args(bl_list)
    bl_list.add_argument(
        "--status",
        choices=("new", "open", "accepted", "resolved"),
        default=None,
        help="show only entries with this status",
    )

    bl_set = bl_sub.add_parser(
        "set", help="record a human verdict: accepted (not a conflict), open, or resolved"
    )
    bl_set.add_argument("fingerprint", help="entry fingerprint (an unambiguous prefix works)")
    bl_set.add_argument("status", choices=("new", "open", "accepted", "resolved"))
    bl_set.add_argument("--note", default=None, help="why — recorded alongside the verdict")
    add_baseline_args(bl_set)

    bl_prune = bl_sub.add_parser(
        "prune", help="delete entries whose finding no longer occurs (missing_since set)"
    )
    add_baseline_args(bl_prune)
    return p


def _run_baseline(args: argparse.Namespace) -> int:
    from .baseline import load_baseline, prune_baseline

    root = Path(args.path).resolve()
    bpath = Path(args.baseline)
    if not bpath.is_absolute():
        bpath = root / bpath
    bl = load_baseline(bpath)
    for w in bl.warnings:
        print(f"warning: {w}", file=sys.stderr)
    if bl.corrupt and args.baseline_command in ("set", "prune"):
        print(
            "error: refusing to modify an unreadable baseline — fix or restore "
            f"{bpath} first (its verdicts would be destroyed by a rewrite)",
            file=sys.stderr,
        )
        return 2

    if args.baseline_command == "list":
        entries = sorted(bl.entries.values(), key=lambda e: (e.status, e.code, e.fingerprint))
        if args.status:
            entries = [e for e in entries if e.status == args.status]
        if not entries:
            print("no baseline entries" + (f" with status '{args.status}'" if args.status else ""))
            return 0
        for e in entries:
            missing = f"  (missing since {_plain(e.missing_since)})" if e.missing_since else ""
            print(f"{_plain(e.fingerprint)}  [{e.status:8s}] {_plain(e.message)}{missing}")
            print(f"    files: {_plain(', '.join(e.files))}")
            if e.note:
                print(f"    note: {_plain(e.note)}")
        if args.status == "new":
            print()
            print(
                "answer each with: detangle baseline set <fingerprint> "
                "accepted|open|resolved --note '...'"
            )
        return 0

    if args.baseline_command == "set":
        matches = [
            e
            for fp, e in bl.entries.items()
            if fp == args.fingerprint or fp.startswith(args.fingerprint)
        ]
        if not matches:
            print(f"error: no baseline entry matches '{args.fingerprint}'", file=sys.stderr)
            return 2
        if len(matches) > 1:
            print(
                f"error: '{args.fingerprint}' is ambiguous ({len(matches)} entries):",
                file=sys.stderr,
            )
            for e in matches:
                print(f"  {e.fingerprint}  {e.message}", file=sys.stderr)
            return 2
        entry = matches[0]
        entry.status = args.status
        if args.note is not None:
            entry.note = args.note
        if not _save_or_report(bl, bpath):
            return 2
        print(
            f"{_plain(entry.fingerprint)} -> {args.status}"
            + (f" ({_plain(entry.note)})" if entry.note else "")
        )
        return 0

    if args.baseline_command == "prune":
        removed = prune_baseline(bl)
        if not _save_or_report(bl, bpath):
            return 2
        print(f"pruned {removed} entr{'y' if removed == 1 else 'ies'} no longer occurring")
        return 0
    return 2  # pragma: no cover


def _save_or_report(bl, bpath: Path) -> bool:
    from .baseline import save_baseline

    try:
        save_baseline(bl, bpath)
    except OSError as exc:
        print(f"error: cannot write {bpath}: {exc}", file=sys.stderr)
        return False
    return True


def _run_scan(args: argparse.Namespace) -> ScanResult:
    root = Path(args.path).resolve()
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        raise SystemExit(2)
    try:
        cfg = load_config(root, args.config)
    except ConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        raise SystemExit(2) from None
    except OSError as e:
        print(f"error: cannot read config: {e}", file=sys.stderr)
        raise SystemExit(2) from None
    if args.nli:
        cfg.lane_nli = True
    if args.jury:
        cfg.lane_jury = True
    if args.screen:
        cfg.lane_screen = True
    if args.deep:
        cfg.deep = True
    if args.baseline is not None:
        if args.baseline != _BASELINE_DEFAULT_SENTINEL:
            cfg.baseline_path = Path(args.baseline)
        elif cfg.baseline_path is None:  # bare --baseline keeps a configured path
            cfg.baseline_path = Path(".detangle-baseline.json")
    if args.update_baseline:
        cfg.update_baseline = True
        if cfg.baseline_path is None:
            cfg.baseline_path = Path(".detangle-baseline.json")
    if cfg.baseline_path is not None:
        resolved = cfg.baseline_path
        if not resolved.is_absolute():
            resolved = cfg.root / resolved
        if resolved.is_dir():
            print(
                f"error: --baseline expects a file, got directory {resolved} "
                "(put the repository path before the flags: detangle scan PATH --baseline)",
                file=sys.stderr,
            )
            raise SystemExit(2)
    if args.only_new:
        cfg.only_new = True
        if cfg.baseline_path is None:
            print("error: --only-new requires --baseline", file=sys.stderr)
            raise SystemExit(2)
    if args.fail_on_new:
        cfg.fail_on_new = True
        if cfg.baseline_path is None:
            print("error: --fail-on-new requires --baseline", file=sys.stderr)
            raise SystemExit(2)
    if args.no_soft:
        cfg.include_soft = False
    if args.fail_on:
        cfg.fail_on = {s.label: s for s in Severity}[args.fail_on]
    if args.select:
        keep = {c.strip().upper() for c in args.select.split(",")}
        unknown = keep - set(RULES)
        if unknown:
            print(f"error: unknown rule code(s): {', '.join(sorted(unknown))}", file=sys.stderr)
            raise SystemExit(2)
        cfg.disabled_rules = frozenset(set(RULES) - keep)
    return scan(cfg)


def _git_lines(root: Path, *args: str) -> list[str] | None:
    """Run git under ``root``; stdout lines on success, None on any failure.

    ``core.quotepath=off`` keeps non-ASCII paths literal instead of
    octal-escaped/quoted, so they compare equal to finding evidence paths.
    """
    try:
        out = subprocess.run(
            ["git", "-c", "core.quotepath=off", *args],
            cwd=root,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return out.stdout.splitlines()


def _changed_files(root: Path, base: str) -> set[str] | None:
    """Files changed vs ``base``, as paths relative to the scan root.

    Returns None when the diff (or repo-prefix translation) cannot be
    computed; the caller then warns and reports all findings.
    """
    lines = _git_lines(root, "diff", "--name-only", f"{base}...HEAD")
    if lines is None:
        lines = _git_lines(root, "diff", "--name-only", base)
        if lines is None:
            return None
    names = {line.strip() for line in lines if line.strip()}
    # git reports paths relative to the REPO root; finding evidence paths are
    # relative to the SCAN root. When scanning a subdirectory of the repo,
    # translate via the scan root's repo prefix (e.g. 'app/').
    prefix_lines = _git_lines(root, "rev-parse", "--show-prefix")
    if prefix_lines is None:
        return None
    prefix = prefix_lines[0].strip() if prefix_lines else ""
    if not prefix:
        return names
    # Changed files outside the scan root cannot match any finding path.
    return {n[len(prefix) :] for n in names if n.startswith(prefix)}


def _emit(result: ScanResult, args: argparse.Namespace) -> None:
    from .report import render_console, render_json, render_markdown, render_sarif

    if args.fmt == "console" and args.output is None:
        render_console(result, verbose=args.verbose)
        return
    text = {
        "json": render_json,
        "sarif": render_sarif,
        "markdown": render_markdown,
    }.get(args.fmt, render_json)(result)
    if args.output:
        try:
            args.output.write_text(text, encoding="utf-8")
        except OSError as e:
            print(f"error: cannot write {args.output}: {e}", file=sys.stderr)
            raise SystemExit(2) from None
        print(f"wrote {args.output}")
    else:
        print(text)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command in (None,):
        parser.print_help()
        return 0

    if args.command == "rules":
        for code, r in sorted(RULES.items()):
            print(f"{code}  {r.name:28s} [{r.default_severity.label:8s}] {r.summary}")
        return 0

    if args.command == "baseline":
        return _run_baseline(args)

    if args.command == "explain":
        code = args.code.upper().split(":")[0]
        r = RULES.get(code)
        if r is None:
            print(f"unknown rule code: {args.code}", file=sys.stderr)
            return 2
        print(f"{r.code} — {r.name} (default severity: {r.default_severity.label})")
        print()
        print(r.summary)
        print()
        print(
            "Docs: https://github.com/DhyeyMavani2003/detangle/blob/main/docs/taxonomy.md"
            f"#{r.code.lower()}-{r.name}"
        )
        return 0

    result = _run_scan(args)

    if args.command == "diff":
        changed = _changed_files(Path(args.path).resolve(), args.base)
        if changed is None:
            print(
                "warning: could not compute git diff; reporting all findings",
                file=sys.stderr,
            )
        else:
            result.findings = [
                f
                for f in result.findings
                if any(ev.span.path in changed for ev in f.evidence)
                or any(u.file.path in changed for u in f.units)
            ]

    _emit(result, args)
    return result.exit_code()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
