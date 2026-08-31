"""GitHub Copilot custom-instructions parser.

Semantics (docs.github.com, verified 2026-08):

- .github/copilot-instructions.md: repo-wide, always in context.
- .github/instructions/NAME.instructions.md: frontmatter ``applyTo`` globs
  (path-scoped); ``excludeAgent`` can hide a file from specific agents.
- Precedence: personal > repository > organization — but "all sets of
  relevant instructions are provided to Copilot": everything CO-ACTIVATES,
  priority is soft. Relative order of repo-wide vs path-specific:
  UNSPECIFIED. Coding-agent guidance: "no longer than 2 pages".
"""

from __future__ import annotations

from ..config import Config
from ..ir import Activation, ActivationMode, ConfigFile, Ecosystem, Layer
from ..markdown import split_frontmatter
from .base import BaseParser, Corpus, read_text, rel

_TWO_PAGES_WORDS = 1000  # ~2 pages of prose


class CopilotParser(BaseParser):
    name = "copilot"

    def parse(self, cfg: Config, corpus: Corpus) -> None:
        root = cfg.root
        main = root / ".github" / "copilot-instructions.md"
        if main.is_file():
            text = read_text(main)
            if text is not None:
                notes: list[str] = []
                words = len(text.split())
                if words > _TWO_PAGES_WORDS:
                    notes.append(
                        f"~{words} words; GitHub guidance for the coding agent is "
                        "'no longer than 2 pages'"
                    )
                cf = ConfigFile(
                    path=rel(root, main),
                    ecosystem=Ecosystem.COPILOT,
                    layer=Layer.PROJECT,
                    tier=20,
                    activation=Activation(mode=ActivationMode.ALWAYS),
                    text=text,
                    mechanism="memory",
                    tool="copilot",
                    notes=notes,
                )
                cf.meta["readers"] = ("copilot",)
                corpus.add(cf)

        instr_dir = root / ".github" / "instructions"
        if instr_dir.is_dir():
            for p in sorted(instr_dir.rglob("*.instructions.md")):
                text = read_text(p)
                if text is None:
                    continue
                meta, body, body_start = split_frontmatter(text)
                apply_to = meta.get("applyTo") or meta.get("applyto")
                globs: tuple[str, ...] = ()
                if isinstance(apply_to, str):
                    globs = tuple(g.strip() for g in apply_to.split(",") if g.strip())
                elif isinstance(apply_to, list):
                    globs = tuple(str(g) for g in apply_to)
                activation = (
                    Activation(mode=ActivationMode.PATH, globs=globs)
                    if globs
                    else Activation(mode=ActivationMode.ALWAYS)
                )
                cf = ConfigFile(
                    path=rel(root, p),
                    ecosystem=Ecosystem.COPILOT,
                    layer=Layer.RULES,
                    tier=20,
                    activation=activation,
                    text=body,
                    meta=dict(meta),
                    mechanism="instructions",
                    tool="copilot",
                )
                cf.meta["readers"] = ("copilot",)
                cf.meta["body_start"] = body_start
                corpus.add(cf)
