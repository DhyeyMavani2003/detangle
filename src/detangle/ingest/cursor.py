"""Cursor rules parser: .cursor/rules/*.mdc (+ legacy .cursorrules).

Semantics (cursor.com/docs/context/rules, verified 2026-08):

- Only ``.mdc`` files count; plain ``.md`` inside .cursor/rules is IGNORED by
  Cursor — flagged as unreachable (DTP06 material).
- Four rule types by frontmatter:
    alwaysApply: true            -> Always (globs/description ignored)
    globs: ...                   -> Auto Attached (path-triggered)
    description: ...             -> Agent Requested (model-triggered)
    none of the above            -> Manual (@-mention only)
- Nested .cursor/rules/ directories scope to their subtree.
- Precedence: Team > Project > User, merge-all with earlier-source-wins as
  soft priority. Ordering among same-level matching rules: UNSPECIFIED.
- Legacy .cursorrules: deprecated but still read; always-on.
"""

from __future__ import annotations

from pathlib import Path

from ..config import Config
from ..ir import Activation, ActivationMode, ConfigFile, Ecosystem, Layer
from ..markdown import split_frontmatter
from .base import BaseParser, Corpus, read_text, rel


def _parse_globs(val: object) -> tuple[str, ...]:
    if isinstance(val, str):
        return tuple(g.strip() for g in val.split(",") if g.strip())
    if isinstance(val, list):
        out: list[str] = []
        for v in val:
            if isinstance(v, str) and v.strip():
                out.append(v.strip())
        return tuple(out)
    return ()


class CursorParser(BaseParser):
    name = "cursor"

    def parse(self, cfg: Config, corpus: Corpus) -> None:
        root = cfg.root
        rules_dirs = [
            root / rp
            for rp in sorted(
                {
                    p.rsplit("/.cursor/rules/", 1)[0]
                    for p in corpus.repo_files
                    if "/.cursor/rules/" in p
                }
            )
        ]
        if (root / ".cursor" / "rules").is_dir():
            rules_dirs.insert(0, root)
        seen: set[Path] = set()
        for base in rules_dirs:
            rules_dir = base / ".cursor" / "rules"
            if not rules_dir.is_dir() or rules_dir in seen:
                continue
            seen.add(rules_dir)
            subtree = "" if base == root else rel(root, base)
            self._parse_rules_dir(cfg, corpus, rules_dir, subtree)

        legacy = root / ".cursorrules"
        if legacy.is_file():
            text = read_text(legacy)
            if text is not None:
                cf = ConfigFile(
                    path=".cursorrules",
                    ecosystem=Ecosystem.CURSOR,
                    layer=Layer.PROJECT,
                    tier=25,
                    activation=Activation(mode=ActivationMode.ALWAYS),
                    text=text,
                    mechanism="memory",
                    tool="cursor",
                    notes=[
                        ".cursorrules is deprecated (still read); migrate to .cursor/rules/*.mdc"
                    ],
                )
                cf.meta["readers"] = ("cursor", "cline")
                corpus.add(cf)

    def _parse_rules_dir(self, cfg: Config, corpus: Corpus, rules_dir: Path, subtree: str) -> None:
        root = cfg.root
        for p in sorted(rules_dir.rglob("*")):
            if not p.is_file():
                continue
            if p.suffix == ".md":
                corpus.notes.append(
                    f"{rel(root, p)}: plain .md inside .cursor/rules is ignored by Cursor "
                    "(only .mdc files load) — rename to .mdc or it never reaches the model"
                )
                continue
            if p.suffix != ".mdc":
                continue
            text = read_text(p)
            if text is None:
                continue
            meta, body, body_start = split_frontmatter(text)
            always = meta.get("alwaysApply") is True or meta.get("always_apply") is True
            globs = _parse_globs(meta.get("globs"))
            desc = str(meta.get("description", "") or "")
            if subtree:
                # nested rules scope to their subtree regardless of type
                scope_prefix = subtree + "/"
                globs = tuple(scope_prefix + g.lstrip("/") for g in globs) or (
                    (f"{subtree}/**",) if always else ()
                )
            if always:
                activation = Activation(
                    mode=ActivationMode.PATH if subtree else ActivationMode.ALWAYS,
                    globs=(f"{subtree}/**",) if subtree else (),
                )
            elif globs:
                activation = Activation(mode=ActivationMode.PATH, globs=globs)
            elif desc:
                activation = Activation(mode=ActivationMode.MODEL, description=desc)
            else:
                activation = Activation(mode=ActivationMode.USER)
            notes: list[str] = []
            if always and (meta.get("globs") or desc):
                notes.append("alwaysApply: true makes Cursor ignore this rule's globs/description")
            n_lines = body.count("\n") + 1
            if n_lines > 500:
                notes.append(f"{n_lines} lines (Cursor guidance: keep rules under 500 lines)")
            cf = ConfigFile(
                path=rel(root, p),
                ecosystem=Ecosystem.CURSOR,
                layer=Layer.RULES,
                tier=20,
                activation=activation,
                text=body,
                meta=dict(meta),
                mechanism="cursor-rule",
                tool="cursor",
                notes=notes,
            )
            cf.meta["readers"] = ("cursor",)
            cf.meta["body_start"] = body_start
            corpus.add(cf)
