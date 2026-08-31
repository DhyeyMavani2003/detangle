"""Reporting: conflict cards (TTY), JSON, SARIF 2.1.0, and Markdown."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

from . import __version__
from .findings import Finding
from .pipeline import ScanResult
from .taxonomy import RULES, Severity

_SEV_STYLE = {
    Severity.ERROR: ("red", "error"),
    Severity.WARNING: ("yellow", "warning"),
    Severity.ADVISORY: ("cyan", "advisory"),
    Severity.INFO: ("dim", "info"),
}


# ---------------------------------------------------------------------------
# Console (rich)
# ---------------------------------------------------------------------------


def render_console(result: ScanResult, verbose: bool = False) -> None:
    from rich.console import Console
    from rich.panel import Panel
    from rich.text import Text

    console = Console()
    s = result.stats
    console.print(
        f"[bold]detangle[/bold] v{__version__} — scanned "
        f"{int(s.get('files', 0))} config files, {int(s.get('units', 0))} instruction "
        f"units, {int(s.get('pairs', 0))} candidate pairs "
        f"({s.get('total_s', 0)}s)"
    )
    console.print()

    if not result.findings:
        console.print("[green]✓ No findings.[/green] Your agent's English is untangled.")
    for f in result.findings:
        color, label = _SEV_STYLE[f.severity]
        title = Text()
        title.append(f"{f.code} ", style=f"bold {color}")
        title.append(f.name, style="bold")
        title.append(f"  [{label}]", style=color)
        bl_tag = result.baseline_tags.get(f.fingerprint)
        if bl_tag == "new":
            title.append("  NEW", style="bold magenta")
        elif bl_tag == "regression":
            title.append("  REGRESSION", style="bold red")
        elif bl_tag == "known":
            title.append("  known", style="dim")

        body = Text()
        body.append(f.message + "\n", style="bold")
        for ev in f.evidence:
            body.append(f"\n  {ev.span}  ", style="dim")
            body.append(f'"{_ellipsize(ev.quote, 160)}"')
            if ev.note:
                body.append(f"  ← {ev.note}", style="italic dim")
        if f.co_activation:
            body.append("\n\n  co-activation: ", style="bold dim")
            body.append(f.co_activation, style="dim")
        if f.precedence:
            body.append("\n  precedence:    ", style="bold dim")
            body.append(f.precedence, style="dim")
        if f.witness:
            body.append("\n  witness:       ", style="bold dim")
            body.append(f.witness, style="dim")
        if f.suggestion:
            body.append("\n  fix:           ", style="bold green")
            body.append(f.suggestion)
        meta = f"lanes: {', '.join(f.lanes)} · confidence: {f.confidence:.0%} · {f.fingerprint}"
        body.append(f"\n  {meta}", style="dim")

        console.print(Panel(body, title=title, title_align="left", border_style=color))

    # Suppression reasons and corpus warnings are user-controlled text: render
    # them as plain Text so bracket sequences are not parsed as rich markup.
    if result.suppressed and verbose:
        console.print(f"[dim]{len(result.suppressed)} finding(s) suppressed:[/dim]")
        for f, sup in result.suppressed:
            console.print(
                Text(f"  {f.fingerprint} — {sup.reason or 'no justification given'}", style="dim")
            )
    if result.warnings and verbose:
        console.print("[dim]notes:[/dim]")
        for w in result.warnings:
            console.print(Text(f"  · {w}", style="dim"))

    counts = result.counts()
    if counts:
        summary = ", ".join(f"{v}× {k}" for k, v in sorted(counts.items()))
        console.print(f"\n[bold]{len(result.findings)} finding(s)[/bold]: {summary}")
        by_sev: dict[str, int] = {}
        for f in result.findings:
            by_sev[f.severity.label] = by_sev.get(f.severity.label, 0) + 1
        console.print(
            "  " + " · ".join(f"{v} {k}" for k, v in sorted(by_sev.items(), reverse=True))
        )
    if result.baseline_stats:
        b = result.baseline_stats
        unchecked = f" · {b['unchecked']} unchecked (lane not run)" if b.get("unchecked") else ""
        console.print(
            f"\n[bold]baseline:[/bold] {b.get('new', 0)} new · "
            f"{b.get('regression', 0)} regression · {b.get('known', 0)} known · "
            f"{b.get('accepted_suppressed', 0)} suppressed by human verdict · "
            f"{b.get('missing', 0)} no longer occurring{unchecked}"
        )


def _ellipsize(text: str, n: int) -> str:
    t = " ".join(text.split())
    return t if len(t) <= n else t[: n - 1] + "…"


# ---------------------------------------------------------------------------
# JSON
# ---------------------------------------------------------------------------


def finding_to_dict(f: Finding) -> dict[str, Any]:
    return {
        "code": f.code,
        "name": f.name,
        "severity": f.severity.label,
        "message": f.message,
        "fingerprint": f.fingerprint,
        "evidence": [
            {
                "path": ev.span.path,
                "start_line": ev.span.start_line,
                "end_line": ev.span.end_line,
                "quote": ev.quote,
                "note": ev.note,
            }
            for ev in f.evidence
        ],
        "co_activation": f.co_activation,
        "precedence": f.precedence,
        "witness": f.witness,
        "suggestion": f.suggestion,
        "confidence": f.confidence,
        "lanes": list(f.lanes),
        "tags": list(f.tags),
    }


def render_json(result: ScanResult) -> str:
    def with_baseline(f: Finding) -> dict[str, Any]:
        d = finding_to_dict(f)
        tag = result.baseline_tags.get(f.fingerprint)
        if tag:
            d["baseline"] = tag
        return d

    doc = {
        "tool": "detangle",
        "version": __version__,
        "stats": result.stats,
        "findings": [with_baseline(f) for f in result.findings],
        "suppressed": [
            {
                "fingerprint": f.fingerprint,
                "code": f.code,
                "reason": sup.reason,
                "pragma": f"{sup.path}:{sup.line}",
            }
            for f, sup in result.suppressed
        ],
        "warnings": result.warnings,
    }
    if result.baseline_stats:
        doc["baseline"] = result.baseline_stats
    return json.dumps(doc, indent=2)


# ---------------------------------------------------------------------------
# SARIF 2.1.0 (GitHub code-scanning native)
# ---------------------------------------------------------------------------


def render_sarif(result: ScanResult) -> str:
    sev_to_level = {
        Severity.ERROR: "error",
        Severity.WARNING: "warning",
        Severity.ADVISORY: "note",
        Severity.INFO: "note",
    }
    rules_seen: dict[str, dict[str, Any]] = {}
    results: list[dict[str, Any]] = []
    for f in result.findings:
        r = RULES.get(f.code)
        if f.code not in rules_seen:
            rules_seen[f.code] = {
                "id": f.code,
                "name": (r.name if r else f.code).replace("-", " ").title().replace(" ", ""),
                "shortDescription": {"text": r.summary if r else f.code},
                "helpUri": "https://github.com/DhyeyMavani2003/detangle/blob/main/docs/taxonomy.md",
                "defaultConfiguration": {
                    "level": sev_to_level[r.default_severity] if r else "warning"
                },
            }
        locations = [
            {
                "physicalLocation": {
                    # SARIF requires a valid RFC 3986 URI reference: percent-
                    # encode spaces/non-ASCII, keeping '/' as the separator.
                    "artifactLocation": {"uri": quote(ev.span.path, safe="/")},
                    "region": {
                        "startLine": ev.span.start_line,
                        "endLine": max(ev.span.end_line, ev.span.start_line),
                    },
                },
                "message": {"text": ev.note or _ellipsize(ev.quote, 120)},
            }
            for ev in f.evidence
        ]
        text = f.message
        if f.co_activation:
            text += f"\nCo-activation: {f.co_activation}"
        if f.precedence:
            text += f"\nPrecedence: {f.precedence}"
        if f.suggestion:
            text += f"\nSuggested fix: {f.suggestion}"
        results.append(
            {
                "ruleId": f.code,
                "level": sev_to_level[f.severity],
                "message": {"text": text},
                "locations": locations[:1] or [_no_location()],
                "relatedLocations": locations[1:],
                "partialFingerprints": {"detangle/v1": f.fingerprint},
            }
        )
    doc = {
        "$schema": "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json",
        "version": "2.1.0",
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": "detangle",
                        "version": __version__,
                        "informationUri": "https://github.com/DhyeyMavani2003/detangle",
                        "rules": list(rules_seen.values()),
                    }
                },
                "results": results,
            }
        ],
    }
    return json.dumps(doc, indent=2)


def _no_location() -> dict[str, Any]:
    return {
        "physicalLocation": {
            "artifactLocation": {"uri": "."},
            "region": {"startLine": 1},
        }
    }


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def render_markdown(result: ScanResult) -> str:
    lines: list[str] = []
    s = result.stats
    lines.append("# detangle report")
    lines.append("")
    lines.append(
        f"Scanned **{int(s.get('files', 0))}** config files → "
        f"**{int(s.get('units', 0))}** instruction units → "
        f"**{int(s.get('pairs', 0))}** candidate pairs → "
        f"**{len(result.findings)}** finding(s)."
    )
    lines.append("")
    if not result.findings:
        lines.append("✅ No findings.")
    if result.baseline_stats:
        b = result.baseline_stats
        unchecked = f" · {b['unchecked']} unchecked (lane not run)" if b.get("unchecked") else ""
        lines.append(
            f"Baseline: **{b.get('new', 0)} new** · {b.get('regression', 0)} regression · "
            f"{b.get('known', 0)} known · {b.get('accepted_suppressed', 0)} suppressed by "
            f"human verdict · {b.get('missing', 0)} no longer occurring{unchecked}."
        )
        lines.append("")
    for f in result.findings:
        bl_tag = result.baseline_tags.get(f.fingerprint)
        marker = {"new": " · **NEW**", "regression": " · **REGRESSION**", "known": " · known"}.get(
            bl_tag or "", ""
        )
        lines.append(f"## {f.code} {f.name} — {f.severity.label}{marker}")
        lines.append("")
        lines.append(f.message)
        lines.append("")
        for ev in f.evidence:
            lines.append(f"- `{ev.span}`" + (f" ({ev.note})" if ev.note else ""))
            lines.append(f"  > {_ellipsize(ev.quote, 300)}")
        if f.co_activation:
            lines.append(f"- **Co-activation:** {f.co_activation}")
        if f.precedence:
            lines.append(f"- **Precedence:** {f.precedence}")
        if f.witness:
            lines.append(f"- **Witness scenario:** {f.witness}")
        if f.suggestion:
            lines.append(f"- **Suggested fix:** {f.suggestion}")
        lines.append(
            f"- _lanes: {', '.join(f.lanes)} · confidence {f.confidence:.0%} · `{f.fingerprint}`_"
        )
        lines.append("")
    if result.suppressed:
        lines.append("## Suppressed")
        lines.append("")
        for f, sup in result.suppressed:
            lines.append(
                f"- `{f.fingerprint}` — {sup.reason or '⚠ no justification given'} "
                f"(`{sup.path}:{sup.line}`)"
            )
        lines.append("")
    if result.warnings:
        # nightly CI reads exactly this rendering — degradation notices
        # (a skipped lane, an unwritable baseline) must not be invisible there
        lines.append("## Notes")
        lines.append("")
        for w in result.warnings:
            lines.append(f"- {w}")
        lines.append("")
    return "\n".join(lines)
