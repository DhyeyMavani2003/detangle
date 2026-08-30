"""Discovery: walk the repo, run ecosystem parsers, build the corpus."""

from __future__ import annotations

from ..config import Config
from ..extract import extract_units
from ..ir import ConfigFile, InstructionUnit
from .agentsmd import AgentsMdParser
from .base import (
    BaseParser,
    Corpus,
    apply_zed_first_match,
    discover_known_commands,
    walk_repo,
)
from .claude_code import ClaudeCodeParser
from .copilot import CopilotParser
from .cursor import CursorParser

PARSERS: dict[str, type[BaseParser]] = {
    "claude-code": ClaudeCodeParser,
    "agents-md": AgentsMdParser,
    "cursor": CursorParser,
    "copilot": CopilotParser,
}


def discover(cfg: Config) -> Corpus:
    """Run all enabled ecosystem parsers over the repo."""
    corpus = Corpus(root=cfg.root)
    corpus.repo_files = set(walk_repo(cfg.root, cfg.ignore_globs, cfg.respect_gitignore))
    corpus.known_commands = discover_known_commands(cfg.root, corpus.repo_files)
    for name in cfg.ecosystems:
        parser_cls = PARSERS.get(name)
        if parser_cls is None:
            corpus.notes.append(f"unknown ecosystem '{name}' — skipped")
            continue
        parser_cls().parse(cfg, corpus)
    apply_zed_first_match(corpus)
    return corpus


def extract_all_units(corpus: Corpus) -> list[InstructionUnit]:
    """Extract instruction units from every discovered config file."""
    units: list[InstructionUnit] = []
    for cf in corpus.files:
        start = int(cf.meta.get("body_start", 1))
        units.extend(extract_units(cf, body_start_line=start))
    return units


__all__ = [
    "PARSERS",
    "Corpus",
    "ConfigFile",
    "discover",
    "extract_all_units",
]
