"""Shared test fixtures: a config-tree scan factory and finding assertions."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from detangle.config import Config
from detangle.findings import Finding
from detangle.pipeline import ScanResult, scan

ScanFactory = Callable[..., ScanResult]


def write_tree(root: Path, files: dict[str, str]) -> None:
    """Write a config tree (repo-relative path -> file text) under ``root``."""
    for relpath, text in files.items():
        p = root / relpath
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")


@pytest.fixture
def scan_factory(tmp_path: Path) -> ScanFactory:
    """Factory: write ``files`` into tmp_path and run the full pipeline over it.

    Keyword arguments become ``Config`` field overrides
    (e.g. ``conflict_budget=0``). Calling the factory again with the same
    files re-scans the same tree (used by the determinism test).
    """

    def _scan(files: dict[str, str], **overrides: Any) -> ScanResult:
        write_tree(tmp_path, files)
        return scan(Config(root=tmp_path, **overrides))

    return _scan


def findings_with_code(result: ScanResult, code: str) -> list[Finding]:
    return [f for f in result.findings if f.code == code]


def assert_finding(result: ScanResult, code: str, *quote_substrings: str) -> Finding:
    """Assert a finding with ``code`` exists whose evidence quotes contain every
    given substring (each substring must appear in at least one quote of the
    same finding). Returns the first matching finding."""
    candidates = findings_with_code(result, code)
    for f in candidates:
        quotes = [ev.quote for ev in f.evidence]
        if all(any(sub in q for q in quotes) for sub in quote_substrings):
            return f
    seen = [(f.code, [ev.quote for ev in f.evidence]) for f in result.findings]
    raise AssertionError(
        f"no {code} finding with evidence quotes containing {list(quote_substrings)}; "
        f"findings were: {seen}"
    )


def assert_no_finding(result: ScanResult, *codes: str) -> None:
    """Assert that none of the given codes fired."""
    hits = [f for f in result.findings if f.code in set(codes)]
    assert not hits, f"expected none of {codes} to fire, but got: " + "; ".join(
        f"{f.code}: {f.message}" for f in hits
    )
