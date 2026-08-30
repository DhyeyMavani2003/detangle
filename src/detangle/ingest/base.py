"""Shared parser infrastructure: the corpus, discovery context, readers matrix."""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..config import Config
from ..ir import ConfigFile

# Which tools read which config surfaces. Drives cross-tool co-activation:
# two files are co-active under tool T only if T reads both. Sources:
# appendix/07 ecosystem map (verified against primary docs, 2026-08).
#
# NOTE: Claude Code does NOT read AGENTS.md (issue #6235 closed unsupported).
# Zed reads only the FIRST match of its 9-name list — applied as a post-pass.

ZED_SEARCH_ORDER = (
    ".rules",
    ".cursorrules",
    ".windsurfrules",
    ".clinerules",
    ".github/copilot-instructions.md",
    "AGENT.md",
    "AGENTS.md",
    "CLAUDE.md",
    "GEMINI.md",
)


@dataclass
class Corpus:
    """Everything discovery produced."""

    root: Path
    files: list[ConfigFile] = field(default_factory=list)
    repo_files: set[str] = field(default_factory=set)  # repo-relative posix paths
    known_commands: set[str] = field(default_factory=set)  # scripts/targets/bins
    notes: list[str] = field(default_factory=list)

    def add(self, cf: ConfigFile) -> None:
        self.files.append(cf)


def rel(root: Path, p: Path) -> str:
    return p.relative_to(root).as_posix()


_DEFAULT_SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".tox",
    "dist",
    "build",
    ".next",
    ".cache",
    "target",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "site-packages",
}


def _load_gitignore(root: Path) -> tuple[str, ...]:
    """Top-level .gitignore patterns (negations skipped — precision-first)."""
    gi = root / ".gitignore"
    if not gi.is_file():
        return ()
    patterns: list[str] = []
    try:
        for line in gi.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("!"):
                continue
            patterns.append(line)
    except OSError:
        return ()
    return tuple(patterns)


def walk_repo(
    root: Path, ignore_globs: tuple[str, ...] = (), respect_gitignore: bool = True
) -> list[str]:
    """Repo-relative posix paths of all files, skipping vendored/derived dirs."""
    from ..globs import glob_match

    gitignore = _load_gitignore(root) if respect_gitignore else ()
    out: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _DEFAULT_SKIP_DIRS]
        if gitignore:
            dirnames[:] = [
                d
                for d in dirnames
                if not any(glob_match(g, rel(root, Path(dirpath) / d) + "/x") for g in gitignore)
            ]
        for fn in filenames:
            p = Path(dirpath) / fn
            r = rel(root, p)
            if any(fnmatch.fnmatch(r, g) for g in ignore_globs):
                continue
            if gitignore and any(glob_match(g, r) for g in gitignore):
                continue
            out.append(r)
    return sorted(out)


def read_text(path: Path, max_bytes: int = 8 * 1024 * 1024) -> str | None:
    """Read a config file defensively; None on binary/unreadable/oversized."""
    try:
        if path.stat().st_size > max_bytes:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in data[:4096]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace")


def discover_known_commands(root: Path, repo_files: set[str]) -> set[str]:
    """Command names the repo actually defines (for DTR05 stale references)."""
    cmds: set[str] = set()
    pkg = root / "package.json"
    if pkg.is_file():
        import json

        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
            scripts = data.get("scripts", {})
            if isinstance(scripts, dict):
                for name in scripts:
                    cmds.add(f"npm run {name}")
                    cmds.add(f"yarn {name}")
                    cmds.add(f"pnpm {name}")
                    cmds.add(f"pnpm run {name}")
                    cmds.add(f"bun run {name}")
        except (json.JSONDecodeError, OSError):
            pass
    mk = root / "Makefile"
    if mk.is_file():
        text = read_text(mk) or ""
        for m in re.finditer(r"^([A-Za-z0-9_.-]+)\s*:(?!=)", text, re.MULTILINE):
            cmds.add(f"make {m.group(1)}")
    for f in ("pyproject.toml",):
        p = root / f
        if p.is_file():
            text = read_text(p) or ""
            in_scripts = False
            for line in text.splitlines():
                if re.match(r"\[(project\.scripts|tool\.poetry\.scripts)\]", line.strip()):
                    in_scripts = True
                    continue
                if line.strip().startswith("["):
                    in_scripts = False
                if in_scripts:
                    m = re.match(r"([A-Za-z0-9_-]+)\s*=", line.strip())
                    if m:
                        cmds.add(m.group(1))
    just = root / "justfile"
    if just.is_file():
        text = read_text(just) or ""
        for m in re.finditer(r"^([A-Za-z0-9_-]+)(?:\s+[^:]*)?:(?!=)", text, re.MULTILINE):
            cmds.add(f"just {m.group(1)}")
    return cmds


def apply_zed_first_match(corpus: Corpus) -> None:
    """Zed loads only the first file from its search list at worktree root."""
    present = [name for name in ZED_SEARCH_ORDER if name in corpus.repo_files]
    if not present:
        return
    winner = present[0]
    for cf in corpus.files:
        readers = set(cf.meta.get("readers", ()))
        if cf.path == winner:
            readers.add("zed")
        else:
            readers.discard("zed")
        cf.meta["readers"] = tuple(sorted(readers))
    if len(present) > 1:
        corpus.notes.append(
            f"Zed reads only {winner} (first match in its search order); "
            f"ignored by Zed: {', '.join(present[1:])}"
        )


class BaseParser:
    """Parser protocol: discover ConfigFiles for one ecosystem."""

    name = "base"

    def parse(self, cfg: Config, corpus: Corpus) -> None:  # pragma: no cover
        raise NotImplementedError
