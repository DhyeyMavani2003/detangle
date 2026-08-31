"""AGENTS.md family parser (the agents.md standard + AGENT.md legacy).

One parser covers Codex, Jules, Amp, Zed, OpenCode, Copilot, Cursor-fallback
and friends. Semantics that matter for linting (per agents.md + per-tool docs):

- "Closest file takes precedence" but ancestor MERGE semantics are
  UNSPECIFIED and implementations diverge: Codex concatenates root->cwd
  (positional, "closer files appear later in the combined prompt"), Copilot
  nearest-wins, Zed loads one file total, Jules root-only. We model the
  Codex concat reading (most permissive w.r.t. co-activation) and flag
  material divergence separately (DTP05).
- Codex ``project_doc_max_bytes`` defaults to 32 KiB and discovery HALTS at
  the limit — deeper files are silently dropped -> BudgetRisk.TRUNCATION.
"""

from __future__ import annotations

from ..config import Config
from ..ir import Activation, ActivationMode, BudgetRisk, ConfigFile, Ecosystem, Layer
from .base import BaseParser, Corpus, read_text

CODEX_PROJECT_DOC_MAX_BYTES = 32 * 1024

# Tools that read AGENTS.md hierarchies (zed handled by first-match post-pass)
AGENTS_MD_READERS = ("codex", "copilot", "cursor", "opencode", "jules", "amp")


class AgentsMdParser(BaseParser):
    name = "agents-md"

    def parse(self, cfg: Config, corpus: Corpus) -> None:
        root = cfg.root
        candidates = [
            rp
            for rp in sorted(corpus.repo_files)
            if rp == "AGENTS.md"
            or rp == "AGENT.md"
            or rp.endswith("/AGENTS.md")
            or rp.endswith("/AGENT.md")
        ]
        # depth-first order approximates Codex root->cwd concatenation
        candidates.sort(key=lambda rp: (rp.count("/"), rp))
        cumulative = 0
        for order, rp in enumerate(candidates):
            p = root / rp
            text = read_text(p)
            if text is None:
                continue
            size = len(text.encode("utf-8"))
            notes: list[str] = []
            risk = BudgetRisk.NONE
            note = ""
            if cumulative >= CODEX_PROJECT_DOC_MAX_BYTES:
                risk = BudgetRisk.TRUNCATION
                note = (
                    "beyond Codex's 32 KiB project_doc_max_bytes cumulative budget — "
                    "silently dropped by Codex discovery"
                )
            elif cumulative + size > CODEX_PROJECT_DOC_MAX_BYTES:
                risk = BudgetRisk.TRUNCATION
                note = (
                    f"crosses Codex's 32 KiB project_doc_max_bytes budget at "
                    f"{CODEX_PROJECT_DOC_MAX_BYTES - cumulative} bytes in — tail is dropped"
                )
            cumulative += size

            is_root = "/" not in rp
            if is_root:
                layer, tier = Layer.PROJECT, 20
                activation = Activation(
                    mode=ActivationMode.ALWAYS, budget_risk=risk, budget_note=note
                )
            else:
                subdir = rp.rsplit("/", 1)[0]
                layer, tier = Layer.SUBDIR, 30
                activation = Activation(
                    mode=ActivationMode.PATH,
                    globs=(f"{subdir}/**",),
                    budget_risk=risk,
                    budget_note=note or f"applies when working under {subdir}/",
                )
            if rp.endswith("AGENT.md"):
                notes.append("AGENT.md is the legacy Amp name; the standard filename is AGENTS.md")
            cf = ConfigFile(
                path=rp,
                ecosystem=Ecosystem.AGENTS_MD,
                layer=layer,
                tier=tier,
                activation=activation,
                text=text,
                mechanism="memory",
                tool="agents-md",
                load_order=order,
                notes=notes,
            )
            readers = AGENTS_MD_READERS if rp.endswith("AGENTS.md") else ("amp",)
            if "/" in rp:
                # Jules reads root only
                readers = tuple(r for r in readers if r != "jules")
            cf.meta["readers"] = readers
            cf.meta["bytes"] = size
            corpus.add(cf)
