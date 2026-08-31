"""Persistent triage baseline: carry human verdicts across scan runs.

The human-in-the-loop workflow this module powers:

1. An overnight scan runs detangle and writes its findings into a baseline
   JSON artifact (checked into git alongside the configs it describes).
2. A human answers each entry by setting ``status`` (and optionally
   ``note``): ``accepted`` means "not a conflict, stop showing me this",
   ``resolved`` means "fixed", ``open`` means "real, being worked on".
3. The next run merges fresh findings against the artifact: verdicts are
   pre-filled, ``accepted`` findings are suppressed, ``resolved`` findings
   that reappear are flagged as regressions, and only genuinely NEW
   findings demand attention.

Identity is two-tiered. The exact ``Finding.fingerprint`` ("CODE:hash") is
tried first; failing that, a code-independent ``pair_key`` — built from
content-addressed unit uids, or from normalized evidence quotes for
unit-less findings — re-attaches the human's verdict when a lane
re-classifies the same pair under a sibling code, or when a unit-less
finding merely shifts lines.

Serialization is byte-deterministic: the artifact lives in git, so the same
logical content must always produce identical bytes.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from .findings import Finding

STATUSES = ("new", "open", "accepted", "resolved")

# Codes that in practice re-classify into each other between runs (an LLM
# lane may call the same pair DTC01 one night and DTC02 the next); a
# pair_key match within this family keeps the human verdict attached.
CONFLICT_FAMILY = frozenset({"DTC01", "DTC02", "DTC03", "DTP03", "DTR01"})

_QUOTE_CAP = 200


@dataclass
class BaselineEntry:
    """One triaged finding as persisted in the artifact.

    ``status``, ``note`` and ``first_seen`` belong to the human/history;
    every other descriptive field is refreshed from the latest scan.
    ``lanes`` records which analysis lanes produced the finding, so a
    cheaper later run (deterministic-only CI after a deep nightly) does not
    mark deep-lane findings as gone.
    """

    fingerprint: str
    pair_key: str
    code: str
    status: str
    note: str = ""
    message: str = ""
    severity: str = ""
    files: list[str] = field(default_factory=list)
    quotes: list[str] = field(default_factory=list)
    lanes: list[str] = field(default_factory=list)
    first_seen: str = ""
    missing_since: str | None = None


@dataclass
class Baseline:
    entries: dict[str, BaselineEntry] = field(default_factory=dict)  # keyed by fingerprint
    warnings: list[str] = field(default_factory=list)
    version: int = 1
    # True when a baseline FILE existed but could not be read/parsed. Writers
    # must refuse to save over it — overwriting would destroy every human
    # verdict the unreadable file still physically contains.
    corrupt: bool = False


@dataclass
class BaselineOutcome:
    """Result of merging one run's findings against the baseline."""

    findings: list[Finding]  # input order, minus accepted-suppressed
    tags: dict[str, str]  # fingerprint -> new | known | regression | accepted
    counts: dict[str, int]
    baseline: Baseline


def today() -> str:
    """Run date; DETANGLE_TODAY overrides for reproducible runs and tests."""
    return os.environ.get("DETANGLE_TODAY") or datetime.date.today().isoformat()


def finding_pair_key(f: Finding) -> str:
    """Code-independent identity for what a finding is *about*.

    Survives verdict-code drift (the taxonomy code is not part of the key)
    and line moves (unit uids are content-addressed; unit-less findings key
    on path + whitespace-normalized quote rather than line numbers).
    """
    if f.units:
        return "+".join(sorted(u.uid for u in f.units))
    if not f.evidence:
        # no units and no evidence: there is no content to key on, and a
        # shared constant key would let unrelated findings adopt each
        # other's verdicts — such findings have exact-fingerprint identity only
        return ""
    basis = "\x00".join(sorted(f"{ev.span.path}:{' '.join(ev.quote.split())}" for ev in f.evidence))
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


def _str(value: object) -> str:
    return value if isinstance(value, str) else ""


def _str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [x for x in value if isinstance(x, str)]


def load_baseline(path: Path) -> Baseline:
    """Read a baseline artifact. Never raises.

    A missing file is simply an empty baseline; anything unreadable or
    misshapen degrades to an empty baseline with a warning, and malformed
    or unknown-status entries are repaired or skipped with a warning.
    """
    b = Baseline()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return b
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        b.warnings.append(f"baseline {path}: unreadable ({exc}); refusing to overwrite it")
        b.corrupt = True
        return b
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        b.warnings.append(f"baseline {path}: corrupt JSON ({exc}); refusing to overwrite it")
        b.corrupt = True
        return b
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        b.warnings.append(f"baseline {path}: unexpected shape; refusing to overwrite it")
        b.corrupt = True
        return b
    if isinstance(data.get("version"), int):
        b.version = data["version"]
    for i, item in enumerate(data["entries"]):
        if not isinstance(item, dict):
            b.warnings.append(f"baseline {path}: entry #{i} is not an object; skipped")
            continue
        fp = item.get("fingerprint")
        if not isinstance(fp, str) or not fp:
            b.warnings.append(f"baseline {path}: entry #{i} has no fingerprint; skipped")
            continue
        status = item.get("status", "new")
        if status not in STATUSES:
            b.warnings.append(
                f"baseline {path}: entry {fp} has unknown status {status!r}; treating as 'new'"
            )
            status = "new"
        missing = item.get("missing_since")
        b.entries[fp] = BaselineEntry(
            fingerprint=fp,
            pair_key=_str(item.get("pair_key")),
            code=_str(item.get("code")),
            status=status,
            note=_str(item.get("note")),
            message=_str(item.get("message")),
            severity=_str(item.get("severity")),
            files=_str_list(item.get("files")),
            quotes=_str_list(item.get("quotes")),
            lanes=_str_list(item.get("lanes")),
            first_seen=_str(item.get("first_seen")),
            missing_since=missing if isinstance(missing, str) else None,
        )
    return b


def save_baseline(b: Baseline, path: Path) -> None:
    """Write the artifact with deterministic bytes — it is checked into git,
    so identical logical content must never churn the file."""
    payload = {
        "version": 1,
        "tool": "detangle",
        "entries": [
            {
                "fingerprint": e.fingerprint,
                "pair_key": e.pair_key,
                "code": e.code,
                "status": e.status,
                "note": e.note,
                "message": e.message,
                "severity": e.severity,
                "files": e.files,
                "quotes": e.quotes,
                "lanes": e.lanes,
                "first_seen": e.first_seen,
                "missing_since": e.missing_since,
            }
            for e in sorted(b.entries.values(), key=lambda e: (e.code, e.fingerprint))
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")


def _refresh(entry: BaselineEntry, f: Finding, adopted: bool) -> None:
    """Refresh the descriptive fields from the latest sighting.

    Severity, files, lanes, and the pair_key always track the latest scan.
    ``message``/``quotes`` are refreshed for deterministic findings (their
    wording only changes when the underlying text changed) and on adoption,
    but are KEPT for LLM-lane findings sighted under the same fingerprint:
    a jury re-verdict words its message differently every run, and the
    checked-in artifact must not churn on quiet nights.
    """
    entry.pair_key = finding_pair_key(f)
    entry.severity = f.severity.label
    entry.files = sorted({ev.span.path for ev in f.evidence} | {u.file.path for u in f.units})
    entry.lanes = sorted(f.lanes)
    if adopted or not entry.message or f.lanes == ("deterministic",):
        entry.message = f.message
        entry.quotes = [" ".join(ev.quote.split())[:_QUOTE_CAP] for ev in f.evidence]


def _adopt(baseline: Baseline, f: Finding, fp: str, claimed: set[str]) -> BaselineEntry | None:
    """Fallback match on pair_key, re-keying the entry to the new fingerprint.

    This keeps a human verdict attached when a lane re-classifies the same
    pair under a sibling CONFLICT_FAMILY code, or when a unit-less finding's
    anchor line shifts (same code, same quotes, new fingerprint).

    Only entries not already claimed by an exact match are candidates. When
    several candidates share the pair_key, prefer the exact code, then the
    exact message; if the choice is STILL ambiguous, refuse — an arbitrary
    pick could silently hand one finding another's human verdict, and "shows
    up as new again" is the safe failure.
    """
    pair_key = finding_pair_key(f)
    if not pair_key:
        return None
    cands = [
        (old_fp, entry)
        for old_fp, entry in baseline.entries.items()
        if old_fp not in claimed
        and entry.pair_key == pair_key
        and (entry.code == f.code or (entry.code in CONFLICT_FAMILY and f.code in CONFLICT_FAMILY))
    ]
    if len(cands) > 1:
        same_code = [c for c in cands if c[1].code == f.code]
        if same_code:
            cands = same_code
    if len(cands) > 1:
        same_msg = [c for c in cands if c[1].message == f.message]
        if same_msg:
            cands = same_msg
    if len(cands) != 1:
        return None
    old_fp, entry = cands[0]
    del baseline.entries[old_fp]
    entry.fingerprint = fp
    entry.code = f.code
    baseline.entries[fp] = entry
    return entry


def apply_baseline(
    findings: list[Finding],
    baseline: Baseline,
    run_date: str,
    ran_lanes: set[str] | None = None,
    disabled_codes: frozenset[str] = frozenset(),
) -> BaselineOutcome:
    """Merge one run's (post-dedupe) findings against the baseline.

    Mutates ``baseline`` in place: matched entries are refreshed, unseen
    findings become new entries, and entries no finding matched get
    ``missing_since`` stamped (once). Entries are never auto-deleted —
    that is ``prune_baseline``'s explicit job.

    Matching is two-phase: every exact fingerprint match is claimed FIRST,
    and only then may leftover findings adopt leftover entries by pair_key —
    so a sibling-code finding can never steal the entry that byte-exactly
    belongs to a finding later in the list.

    ``ran_lanes``/``disabled_codes`` guard the missing stamp: an entry whose
    lanes did not all run this time (a deterministic-only CI run after a
    deep nightly), or whose rule is disabled (``--select``), is counted as
    ``unchecked`` and keeps its state — a cheaper run proves nothing about
    it, and stamping it would make ``prune`` destroy human verdicts.
    """
    tags: dict[str, str] = {}
    counts = {
        "new": 0,
        "known": 0,
        "regression": 0,
        "accepted_suppressed": 0,
        "missing": 0,
        "unchecked": 0,
    }
    kept: list[Finding] = []
    claimed: set[str] = set()

    # phase 1: exact fingerprint matches claim their entries
    resolved: dict[int, tuple[BaselineEntry, bool]] = {}  # index -> (entry, adopted)
    for i, f in enumerate(findings):
        entry = baseline.entries.get(f.fingerprint)
        if entry is not None and f.fingerprint not in claimed:
            resolved[i] = (entry, False)
            claimed.add(f.fingerprint)

    # phase 2: the rest may adopt unclaimed entries by pair_key (re-keying)
    for i, f in enumerate(findings):
        if i in resolved:
            continue
        entry = _adopt(baseline, f, f.fingerprint, claimed)
        if entry is not None:
            resolved[i] = (entry, True)
            claimed.add(entry.fingerprint)

    for i, f in enumerate(findings):
        fp = f.fingerprint
        entry, adopted = resolved.get(i, (None, True))
        if entry is None:
            entry = BaselineEntry(
                fingerprint=fp,
                pair_key=finding_pair_key(f),
                code=f.code,
                status="new",
                first_seen=run_date,
            )
            baseline.entries[fp] = entry
            claimed.add(fp)
            # adopted=True: new entries always take the finding's wording
        entry.missing_since = None
        _refresh(entry, f, adopted)
        if entry.status == "accepted":
            # the human said not-a-conflict: suppress, but still record the tag
            tags[fp] = "accepted"
            counts["accepted_suppressed"] += 1
            continue
        if entry.status == "resolved":
            # the human said fixed, yet it is back: a regression, untriaged again
            entry.status = "new"
            tags[fp] = "regression"
            counts["regression"] += 1
        elif entry.status == "open":
            tags[fp] = "known"
            counts["known"] += 1
        else:
            tags[fp] = "new"
            counts["new"] += 1
        kept.append(f)

    for fp, entry in baseline.entries.items():
        if fp in claimed:
            continue
        lanes_needed = set(entry.lanes) or {"deterministic"}
        if (ran_lanes is not None and not lanes_needed <= ran_lanes) or (
            entry.code in disabled_codes
        ):
            counts["unchecked"] += 1  # this run couldn't have seen it; no verdict
            continue
        counts["missing"] += 1
        if entry.missing_since is None:  # stamp once; identical reruns must not churn
            entry.missing_since = run_date

    return BaselineOutcome(findings=kept, tags=tags, counts=counts, baseline=baseline)


def prune_baseline(b: Baseline) -> int:
    """Remove entries stamped ``missing_since`` (no longer found). Returns
    how many were removed."""
    gone = [fp for fp, e in b.entries.items() if e.missing_since is not None]
    for fp in gone:
        del b.entries[fp]
    return len(gone)
