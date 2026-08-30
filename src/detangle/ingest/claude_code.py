"""Claude Code ecosystem parser.

Surfaces (semantics per code.claude.com docs, verified 2026-08):

- CLAUDE.md hierarchy: user ~/.claude/CLAUDE.md -> project ./CLAUDE.md or
  ./.claude/CLAUDE.md -> CLAUDE.local.md; concatenation root->cwd (positional,
  NOT override: "if two rules contradict each other, Claude may pick one
  arbitrarily"). Subdirectory CLAUDE.md loads on demand when files there are
  read -> PATH activation over that subtree.
- @imports: relative to the importing file, max depth 4 hops, code spans
  skipped.
- .claude/rules/*.md (recursive): frontmatter ``paths:`` globs -> PATH
  activation; without paths -> launch set. User rules load before project
  rules (positional: project higher).
- .claude/skills/*/SKILL.md: model-triggered via description (+ when_to_use);
  combined description truncated at 1,536 chars in the skill listing.
  Name-shadowing precedence: enterprise > personal > PROJECT (< personal!).
- .claude/agents/*.md subagents: model-triggered; PROJECT > user (polarity
  flip vs skills). Subagent bodies run in separate contexts: two subagents'
  units never co-activate with each other, but each co-activates with the
  CLAUDE.md hierarchy (subagents receive it).
- .claude/commands/*.md: user-invoked.

Tier scheme (lower = higher precedence within a mechanism):
  memory: managed=0, user=10, project=20, local=25, subdir=30
  skills: enterprise=0, personal=10, project=20, plugin=30
  subagents: managed=0, project=10, user=20, plugin=30
"""

from __future__ import annotations

import re
from pathlib import Path

from ..config import Config
from ..ir import Activation, ActivationMode, BudgetRisk, ConfigFile, Ecosystem, Layer
from ..markdown import split_frontmatter
from .base import BaseParser, Corpus, read_text, rel

MAX_IMPORT_DEPTH = 4
SKILL_LISTING_CAP = 1536  # chars: description + when_to_use in the skill listing
CLAUDE_MD_LINE_GUIDANCE = 200

_IMPORT_RE = re.compile(r"(?<![\w`@])@((?:/|\.\.?/(?:\.\.?/)*)?[\w~][\w./~-]*[\w])")
_INLINE_CODE_RE = re.compile(r"``+[^`]*``+|`[^`]*`")
_FENCE_OPEN_RE = re.compile(r"^\s*(`{3,}|~{3,})")


def _find_imports(
    root: Path, path: Path, text: str, depth: int, seen: set[Path], notes: list[str]
) -> list[tuple[Path, str]]:
    """Collect @imported files (recursively, ≤4 hops) as (path, text) pairs.

    Imported files are kept as separate ConfigFiles (so line spans stay
    correct) carrying the importer's activation and tier.
    """
    if depth >= MAX_IMPORT_DEPTH:
        notes.append(f"{rel(root, path)}: @import depth limit ({MAX_IMPORT_DEPTH} hops) reached")
        return []
    found: list[tuple[Path, str]] = []
    fence_close: re.Pattern[str] | None = None  # set while inside a fence
    for line in text.split("\n"):
        if fence_close is not None:
            if fence_close.match(line):
                fence_close = None
            continue
        fm = _FENCE_OPEN_RE.match(line)
        if fm:
            marker = fm.group(1)
            # a closing fence is the marker alone, with a same-or-longer run
            fence_close = re.compile(rf"^\s*{re.escape(marker[0])}{{{len(marker)},}}\s*$")
            continue
        # inline code spans are not imports (`cat @file.md` is an example)
        scannable = _INLINE_CODE_RE.sub(" ", line)
        for m in _IMPORT_RE.finditer(scannable):
            target = m.group(1)
            if target.startswith("~"):
                notes.append(f"{rel(root, path)}: @import outside repo not followed: @{target}")
                continue
            if "." not in target.rsplit("/", 1)[-1]:
                continue  # bare @word is probably a mention, not an import
            if target.startswith("/"):
                notes.append(f"{rel(root, path)}: absolute @import not followed: @{target}")
                continue
            cand = (path.parent / target).resolve()
            try:
                cand.relative_to(root.resolve())
            except ValueError:
                notes.append(f"{rel(root, path)}: @import outside repo not followed: @{target}")
                continue
            if cand in seen:
                continue
            if not cand.is_file():
                notes.append(f"{rel(root, path)}: @import target does not exist: @{target}")
                continue
            seen.add(cand)
            sub = read_text(cand)
            if sub is None:
                continue
            found.append((cand, sub))
            found.extend(_find_imports(root, cand, sub, depth + 1, seen, notes))
    return found


class ClaudeCodeParser(BaseParser):
    name = "claude-code"

    def parse(self, cfg: Config, corpus: Corpus) -> None:
        root = cfg.root
        self._memory_files(cfg, corpus, root)
        self._rules(cfg, corpus, root)
        self._skills(cfg, corpus, root)
        self._subagents(cfg, corpus, root)
        self._commands(cfg, corpus, root)

    # -- CLAUDE.md hierarchy ------------------------------------------------

    def _memory_files(self, cfg: Config, corpus: Corpus, root: Path) -> None:
        load_order = 0

        def add_memory(p: Path, layer: Layer, tier: int, activation: Activation) -> None:
            nonlocal load_order
            text = read_text(p)
            if text is None:
                return
            notes: list[str] = []
            imported = _find_imports(root, p, text, 0, {p.resolve()}, notes)
            n_lines = text.count("\n") + 1
            if n_lines > CLAUDE_MD_LINE_GUIDANCE:
                notes.append(
                    f"{n_lines} lines (Anthropic guidance: target under "
                    f"{CLAUDE_MD_LINE_GUIDANCE} lines)"
                )
            cf = ConfigFile(
                path=rel(root, p),
                ecosystem=Ecosystem.CLAUDE_CODE,
                layer=layer,
                tier=tier,
                activation=activation,
                text=text,
                mechanism="memory",
                tool="claude-code",
                load_order=load_order,
                notes=notes,
            )
            # Copilot also reads a repo-root CLAUDE.md (root only)
            cf.meta["readers"] = (
                ("claude-code", "copilot") if rel(root, p) == "CLAUDE.md" else ("claude-code",)
            )
            corpus.add(cf)
            load_order += 1
            for ip, itext in imported:
                icf = ConfigFile(
                    path=rel(root, ip),
                    ecosystem=Ecosystem.CLAUDE_CODE,
                    layer=layer,
                    tier=tier,
                    activation=activation,
                    text=itext,
                    mechanism="memory",
                    tool="claude-code",
                    load_order=load_order,
                    notes=[f"@imported by {rel(root, p)}"],
                )
                icf.meta["readers"] = ("claude-code",)
                icf.meta["imported_by"] = rel(root, p)
                corpus.add(icf)
                load_order += 1

        # user-global (only when a simulated user dir is provided)
        if cfg.user_dir:
            up = cfg.user_dir / ".claude" / "CLAUDE.md"
            if up.is_file():
                text = read_text(up)
                if text is not None:
                    cf = ConfigFile(
                        path=up.as_posix(),
                        ecosystem=Ecosystem.CLAUDE_CODE,
                        layer=Layer.USER_GLOBAL,
                        tier=10,
                        activation=Activation(mode=ActivationMode.ALWAYS),
                        text=text,
                        mechanism="memory",
                        tool="claude-code",
                        load_order=load_order,
                    )
                    cf.meta["readers"] = ("claude-code",)
                    corpus.add(cf)
                    load_order += 1

        # project root: ./CLAUDE.md or ./.claude/CLAUDE.md
        for name in ("CLAUDE.md", ".claude/CLAUDE.md"):
            p = root / name
            if p.is_file():
                add_memory(p, Layer.PROJECT, 20, Activation(mode=ActivationMode.ALWAYS))
        p = root / "CLAUDE.local.md"
        if p.is_file():
            add_memory(p, Layer.LOCAL, 25, Activation(mode=ActivationMode.ALWAYS))

        # subdirectory CLAUDE.md: on-demand when files in that subtree are read
        for rp in sorted(corpus.repo_files):
            if rp in {"CLAUDE.md", ".claude/CLAUDE.md", "CLAUDE.local.md"}:
                continue
            if rp.endswith("/CLAUDE.md") and "/.claude/" not in rp:
                subdir = rp.rsplit("/", 1)[0]
                add_memory(
                    root / rp,
                    Layer.SUBDIR,
                    30,
                    Activation(
                        mode=ActivationMode.PATH,
                        globs=(f"{subdir}/**",),
                        budget_note=f"loads on demand when files under {subdir}/ are read",
                    ),
                )

    # -- .claude/rules ------------------------------------------------------

    def _rules(self, cfg: Config, corpus: Corpus, root: Path) -> None:
        rules_dir = root / ".claude" / "rules"
        if not rules_dir.is_dir():
            return
        for p in sorted(rules_dir.rglob("*.md")):
            text = read_text(p)
            if text is None:
                continue
            meta, body, body_start = split_frontmatter(text)
            paths = meta.get("paths") or meta.get("globs")
            globs: tuple[str, ...] = ()
            if isinstance(paths, str):
                globs = tuple(g.strip() for g in paths.split(",") if g.strip())
            elif isinstance(paths, list):
                globs = tuple(str(g) for g in paths)
            activation = (
                Activation(mode=ActivationMode.PATH, globs=globs)
                if globs
                else Activation(mode=ActivationMode.ALWAYS)
            )
            cf = ConfigFile(
                path=rel(root, p),
                ecosystem=Ecosystem.CLAUDE_CODE,
                layer=Layer.RULES,
                tier=20,
                activation=activation,
                text=body,
                meta=dict(meta),
                mechanism="rules",
                tool="claude-code",
            )
            cf.meta["readers"] = ("claude-code",)
            cf.meta["body_start"] = body_start
            corpus.add(cf)

    # -- skills -------------------------------------------------------------

    def _skills(self, cfg: Config, corpus: Corpus, root: Path) -> None:
        for skills_dir, tier, layer in ((root / ".claude" / "skills", 20, Layer.SKILL),):
            if not skills_dir.is_dir():
                continue
            for sk in sorted(skills_dir.iterdir()):
                sm = sk / "SKILL.md"
                if not sm.is_file():
                    continue
                text = read_text(sm)
                if text is None:
                    continue
                meta, body, body_start = split_frontmatter(text)
                name = str(meta.get("name", sk.name))
                desc = str(meta.get("description", "") or "")
                when = str(meta.get("when_to_use", "") or "")
                trigger = (desc + " " + when).strip()
                notes: list[str] = []
                risk = BudgetRisk.NONE
                if len(trigger) > SKILL_LISTING_CAP:
                    risk = BudgetRisk.LISTING
                    notes.append(
                        f"combined description+when_to_use is {len(trigger)} chars; "
                        f"Claude Code truncates the skill listing at {SKILL_LISTING_CAP}"
                    )
                body_lines = body.count("\n") + 1
                if body_lines > 500:
                    notes.append(
                        f"skill body is {body_lines} lines (guidance: <=500; "
                        "compaction re-attaches only the first 5,000 tokens)"
                    )
                mode = ActivationMode.MODEL
                if meta.get("disable-model-invocation") is True:
                    mode = ActivationMode.USER
                cf = ConfigFile(
                    path=rel(root, sm),
                    ecosystem=Ecosystem.CLAUDE_CODE,
                    layer=layer,
                    tier=tier,
                    activation=Activation(mode=mode, description=trigger, budget_risk=risk),
                    text=body,
                    meta=dict(meta),
                    mechanism="skill",
                    tool="claude-code",
                    notes=notes,
                )
                cf.meta["skill_name"] = name
                cf.meta["trigger_chars"] = len(trigger)
                cf.meta["readers"] = ("claude-code",)
                cf.meta["body_start"] = body_start
                corpus.add(cf)

    # -- subagents ----------------------------------------------------------

    def _subagents(self, cfg: Config, corpus: Corpus, root: Path) -> None:
        agents_dir = root / ".claude" / "agents"
        if not agents_dir.is_dir():
            return
        for p in sorted(agents_dir.glob("*.md")):
            text = read_text(p)
            if text is None:
                continue
            meta, body, body_start = split_frontmatter(text)
            if not meta:
                corpus.notes.append(
                    f"{rel(root, p)}: subagent has missing/malformed YAML frontmatter — "
                    "Claude Code silently skips such files"
                )
            name = str(meta.get("name", p.stem))
            desc = str(meta.get("description", "") or "")
            cf = ConfigFile(
                path=rel(root, p),
                ecosystem=Ecosystem.CLAUDE_CODE,
                layer=Layer.SUBAGENT,
                tier=10,  # project subagents outrank user ones (polarity flip)
                activation=Activation(mode=ActivationMode.MODEL, description=desc),
                text=body,
                meta=dict(meta),
                mechanism="subagent",
                tool="claude-code",
            )
            cf.meta["agent_name"] = name
            cf.meta["readers"] = ("claude-code",)
            cf.meta["body_start"] = body_start
            # isolated context: never co-active with other subagent bodies
            cf.meta["context_scope"] = f"subagent:{name}"
            corpus.add(cf)

    # -- commands -----------------------------------------------------------

    def _commands(self, cfg: Config, corpus: Corpus, root: Path) -> None:
        cmd_dir = root / ".claude" / "commands"
        if not cmd_dir.is_dir():
            return
        for p in sorted(cmd_dir.rglob("*.md")):
            text = read_text(p)
            if text is None:
                continue
            meta, body, body_start = split_frontmatter(text)
            cf = ConfigFile(
                path=rel(root, p),
                ecosystem=Ecosystem.CLAUDE_CODE,
                layer=Layer.SKILL,
                tier=40,
                activation=Activation(
                    mode=ActivationMode.USER,
                    description=str(meta.get("description", "") or ""),
                ),
                text=body,
                meta=dict(meta),
                mechanism="command",
                tool="claude-code",
            )
            cf.meta["command_name"] = p.stem
            cf.meta["readers"] = ("claude-code",)
            cf.meta["body_start"] = body_start
            corpus.add(cf)
