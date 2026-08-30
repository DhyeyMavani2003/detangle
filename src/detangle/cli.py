"""The detangle command-line interface.

detangle scan [path]           full scan
detangle diff [path] [--base]  findings introduced by changed config files
detangle explain DTC01         rule documentation
detangle rules                 list all rules
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from . import __version__
from .config import ConfigError, load_config
from .pipeline import ScanResult, scan
from .taxonomy import RULES, Severity


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
    return p


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
