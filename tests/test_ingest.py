"""Discovery tests: build config trees in tmp_path and run detangle.ingest.discover."""

from __future__ import annotations

from pathlib import Path

from detangle.config import Config
from detangle.ingest import discover
from detangle.ingest.base import discover_known_commands, walk_repo
from detangle.ir import ActivationMode, BudgetRisk, ConfigFile, Ecosystem, Layer


def write(root: Path, relpath: str, text: str) -> Path:
    p = root / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def by_path(corpus) -> dict[str, ConfigFile]:
    return {cf.path: cf for cf in corpus.files}


def scan_tree(tmp_path: Path):
    return discover(Config(root=tmp_path))


# ---------------------------------------------------------------------------
# Claude Code
# ---------------------------------------------------------------------------


class TestClaudeMemoryHierarchy:
    def test_root_dotclaude_and_local_layers_and_tiers(self, tmp_path: Path) -> None:
        write(tmp_path, "CLAUDE.md", "Always run tests before committing.\n")
        write(tmp_path, ".claude/CLAUDE.md", "Prefer small commits.\n")
        write(tmp_path, "CLAUDE.local.md", "Use my local registry mirror.\n")
        files = by_path(scan_tree(tmp_path))

        root = files["CLAUDE.md"]
        assert (root.ecosystem, root.layer, root.tier) == (Ecosystem.CLAUDE_CODE, Layer.PROJECT, 20)
        assert root.activation.mode == ActivationMode.ALWAYS
        assert root.mechanism == "memory"

        dot = files[".claude/CLAUDE.md"]
        assert (dot.layer, dot.tier) == (Layer.PROJECT, 20)
        assert dot.activation.mode == ActivationMode.ALWAYS

        local = files["CLAUDE.local.md"]
        assert (local.layer, local.tier) == (Layer.LOCAL, 25)
        assert local.activation.mode == ActivationMode.ALWAYS

    def test_root_claude_md_read_by_copilot_too(self, tmp_path: Path) -> None:
        write(tmp_path, "CLAUDE.md", "Run tests.\n")
        write(tmp_path, ".claude/CLAUDE.md", "Small commits.\n")
        files = by_path(scan_tree(tmp_path))
        assert {"claude-code", "copilot"} <= set(files["CLAUDE.md"].meta["readers"])
        assert set(files[".claude/CLAUDE.md"].meta["readers"]) == {"claude-code"}

    def test_subdir_claude_md_gets_path_activation(self, tmp_path: Path) -> None:
        write(tmp_path, "CLAUDE.md", "Root rules.\n")
        write(tmp_path, "src/api/CLAUDE.md", "Use FastAPI conventions here.\n")
        files = by_path(scan_tree(tmp_path))
        sub = files["src/api/CLAUDE.md"]
        assert (sub.layer, sub.tier) == (Layer.SUBDIR, 30)
        assert sub.activation.mode == ActivationMode.PATH
        assert sub.activation.globs == ("src/api/**",)

    def test_import_becomes_its_own_config_file(self, tmp_path: Path) -> None:
        write(tmp_path, "CLAUDE.md", "Root rules.\n\nSee @docs/style.md for conventions.\n")
        write(tmp_path, "docs/style.md", "Never use tabs for indentation.\n")
        files = by_path(scan_tree(tmp_path))

        imp = files["docs/style.md"]
        assert imp.ecosystem == Ecosystem.CLAUDE_CODE
        assert imp.mechanism == "memory"
        assert imp.meta["imported_by"] == "CLAUDE.md"
        # inherits the importer's activation and tier
        assert imp.activation.mode == ActivationMode.ALWAYS
        assert imp.tier == 20
        assert any("@imported by CLAUDE.md" in n for n in imp.notes)
        # loaded after the importer
        assert imp.load_order > files["CLAUDE.md"].load_order

    def test_missing_import_target_yields_note(self, tmp_path: Path) -> None:
        write(tmp_path, "CLAUDE.md", "See @docs/missing.md for more.\n")
        files = by_path(scan_tree(tmp_path))
        root = files["CLAUDE.md"]
        assert any("does not exist" in n and "@docs/missing.md" in n for n in root.notes)
        assert "docs/missing.md" not in files


class TestClaudeRules:
    def test_rule_with_paths_frontmatter_is_path_activated(self, tmp_path: Path) -> None:
        write(
            tmp_path,
            ".claude/rules/python.md",
            '---\npaths:\n  - "src/**/*.py"\n---\nUse type hints everywhere.\n',
        )
        files = by_path(scan_tree(tmp_path))
        cf = files[".claude/rules/python.md"]
        assert (cf.layer, cf.tier, cf.mechanism) == (Layer.RULES, 20, "rules")
        assert cf.activation.mode == ActivationMode.PATH
        assert cf.activation.globs == ("src/**/*.py",)

    def test_rule_without_paths_is_always(self, tmp_path: Path) -> None:
        write(tmp_path, ".claude/rules/general.md", "Never commit secrets.\n")
        files = by_path(scan_tree(tmp_path))
        cf = files[".claude/rules/general.md"]
        assert cf.activation.mode == ActivationMode.ALWAYS
        assert cf.activation.globs == ()


class TestClaudeSkills:
    def test_description_and_when_to_use_combine_into_trigger(self, tmp_path: Path) -> None:
        write(
            tmp_path,
            ".claude/skills/deploy/SKILL.md",
            "---\nname: deploy\ndescription: Deploy the app.\n"
            "when_to_use: When the user asks to ship.\n---\nRun the deploy script.\n",
        )
        files = by_path(scan_tree(tmp_path))
        cf = files[".claude/skills/deploy/SKILL.md"]
        assert cf.mechanism == "skill"
        assert cf.activation.mode == ActivationMode.MODEL
        assert cf.activation.description == "Deploy the app. When the user asks to ship."
        assert cf.activation.budget_risk == BudgetRisk.NONE
        assert cf.meta["skill_name"] == "deploy"

    def test_oversized_trigger_gets_listing_budget_risk(self, tmp_path: Path) -> None:
        long_desc = "deploy the application to production " * 50  # ~1850 chars
        write(
            tmp_path,
            ".claude/skills/big/SKILL.md",
            f"---\nname: big\ndescription: {long_desc.strip()}\n---\nBody.\n",
        )
        files = by_path(scan_tree(tmp_path))
        cf = files[".claude/skills/big/SKILL.md"]
        assert cf.meta["trigger_chars"] > 1536
        assert cf.activation.budget_risk == BudgetRisk.LISTING
        assert any("1536" in n for n in cf.notes)

    def test_disable_model_invocation_makes_skill_user_invoked(self, tmp_path: Path) -> None:
        write(
            tmp_path,
            ".claude/skills/manual/SKILL.md",
            "---\nname: manual\ndescription: Only when asked.\n"
            "disable-model-invocation: true\n---\nBody.\n",
        )
        files = by_path(scan_tree(tmp_path))
        assert files[".claude/skills/manual/SKILL.md"].activation.mode == ActivationMode.USER


class TestClaudeSubagents:
    def test_subagent_gets_context_scope_meta(self, tmp_path: Path) -> None:
        write(
            tmp_path,
            ".claude/agents/reviewer.md",
            "---\nname: reviewer\ndescription: Reviews code for style.\n---\nBe strict.\n",
        )
        corpus = scan_tree(tmp_path)
        cf = by_path(corpus)[".claude/agents/reviewer.md"]
        assert cf.mechanism == "subagent"
        assert cf.layer == Layer.SUBAGENT
        assert cf.activation.mode == ActivationMode.MODEL
        assert cf.activation.description == "Reviews code for style."
        assert cf.meta["context_scope"] == "subagent:reviewer"

    def test_malformed_frontmatter_produces_corpus_note(self, tmp_path: Path) -> None:
        write(
            tmp_path,
            ".claude/agents/broken.md",
            "---\nname: [unclosed\n---\nStill a body.\n",
        )
        corpus = scan_tree(tmp_path)
        assert any(".claude/agents/broken.md" in n and "frontmatter" in n for n in corpus.notes)


class TestClaudeCommands:
    def test_commands_are_user_invoked(self, tmp_path: Path) -> None:
        write(tmp_path, ".claude/commands/ship.md", "---\ndescription: Ship it\n---\nDeploy now.\n")
        files = by_path(scan_tree(tmp_path))
        cf = files[".claude/commands/ship.md"]
        assert cf.mechanism == "command"
        assert cf.activation.mode == ActivationMode.USER
        assert cf.meta["command_name"] == "ship"


# ---------------------------------------------------------------------------
# AGENTS.md family
# ---------------------------------------------------------------------------


class TestAgentsMd:
    def test_root_always_and_subdir_path(self, tmp_path: Path) -> None:
        write(tmp_path, "AGENTS.md", "Run make test before pushing.\n")
        write(tmp_path, "app/AGENTS.md", "Use React function components.\n")
        files = by_path(scan_tree(tmp_path))

        root = files["AGENTS.md"]
        assert (root.ecosystem, root.layer, root.tier) == (Ecosystem.AGENTS_MD, Layer.PROJECT, 20)
        assert root.activation.mode == ActivationMode.ALWAYS
        assert root.load_order == 0

        sub = files["app/AGENTS.md"]
        assert (sub.layer, sub.tier) == (Layer.SUBDIR, 30)
        assert sub.activation.mode == ActivationMode.PATH
        assert sub.activation.globs == ("app/**",)
        assert sub.load_order == 1

    def test_jules_reads_root_only(self, tmp_path: Path) -> None:
        write(tmp_path, "AGENTS.md", "Root guidance.\n")
        write(tmp_path, "app/AGENTS.md", "Subdir guidance.\n")
        files = by_path(scan_tree(tmp_path))
        assert "jules" in files["AGENTS.md"].meta["readers"]
        assert "jules" not in files["app/AGENTS.md"].meta["readers"]
        assert {"codex", "copilot", "cursor"} <= set(files["app/AGENTS.md"].meta["readers"])

    def test_codex_32kib_cumulative_budget_truncation(self, tmp_path: Path) -> None:
        write(tmp_path, "AGENTS.md", "a" * 30_000)
        write(tmp_path, "app/AGENTS.md", "b" * 5_000)  # crosses 32 KiB partway in
        write(tmp_path, "zz/AGENTS.md", "c" * 100)  # entirely beyond the budget
        files = by_path(scan_tree(tmp_path))

        assert files["AGENTS.md"].activation.budget_risk == BudgetRisk.NONE

        crossing = files["app/AGENTS.md"]
        assert crossing.activation.budget_risk == BudgetRisk.TRUNCATION
        assert "crosses" in crossing.activation.budget_note

        beyond = files["zz/AGENTS.md"]
        assert beyond.activation.budget_risk == BudgetRisk.TRUNCATION
        assert "beyond" in beyond.activation.budget_note


# ---------------------------------------------------------------------------
# Cursor
# ---------------------------------------------------------------------------


class TestCursor:
    def test_mdc_rule_types_map_to_activation_modes(self, tmp_path: Path) -> None:
        write(tmp_path, ".cursor/rules/always.mdc", "---\nalwaysApply: true\n---\nAlways on.\n")
        write(tmp_path, ".cursor/rules/py.mdc", '---\nglobs: "**/*.py"\n---\nPython style.\n')
        write(
            tmp_path,
            ".cursor/rules/agent.mdc",
            "---\ndescription: Use when refactoring legacy modules.\n---\nRefactor carefully.\n",
        )
        write(tmp_path, ".cursor/rules/manual.mdc", "Manual rule with no frontmatter.\n")
        files = by_path(scan_tree(tmp_path))

        always = files[".cursor/rules/always.mdc"]
        assert always.ecosystem == Ecosystem.CURSOR
        assert always.mechanism == "cursor-rule"
        assert always.activation.mode == ActivationMode.ALWAYS

        auto = files[".cursor/rules/py.mdc"]
        assert auto.activation.mode == ActivationMode.PATH
        assert auto.activation.globs == ("**/*.py",)

        agent = files[".cursor/rules/agent.mdc"]
        assert agent.activation.mode == ActivationMode.MODEL
        assert agent.activation.description == "Use when refactoring legacy modules."

        manual = files[".cursor/rules/manual.mdc"]
        assert manual.activation.mode == ActivationMode.USER

    def test_plain_md_in_rules_dir_yields_corpus_note(self, tmp_path: Path) -> None:
        write(tmp_path, ".cursor/rules/notes.md", "This never loads.\n")
        corpus = scan_tree(tmp_path)
        assert ".cursor/rules/notes.md" not in by_path(corpus)
        assert any(".cursor/rules/notes.md" in n and "ignored" in n for n in corpus.notes)

    def test_cursorrules_deprecated_note_and_readers(self, tmp_path: Path) -> None:
        write(tmp_path, ".cursorrules", "Old-style rules.\n")
        files = by_path(scan_tree(tmp_path))
        cf = files[".cursorrules"]
        assert cf.activation.mode == ActivationMode.ALWAYS
        assert any("deprecated" in n for n in cf.notes)
        assert {"cursor", "cline"} <= set(cf.meta["readers"])


# ---------------------------------------------------------------------------
# Copilot
# ---------------------------------------------------------------------------


class TestCopilot:
    def test_copilot_instructions_always(self, tmp_path: Path) -> None:
        write(tmp_path, ".github/copilot-instructions.md", "Write idiomatic Go.\n")
        files = by_path(scan_tree(tmp_path))
        cf = files[".github/copilot-instructions.md"]
        assert cf.ecosystem == Ecosystem.COPILOT
        assert cf.activation.mode == ActivationMode.ALWAYS
        assert "copilot" in cf.meta["readers"]

    def test_scoped_instructions_apply_to_globs(self, tmp_path: Path) -> None:
        write(
            tmp_path,
            ".github/instructions/api.instructions.md",
            '---\napplyTo: "src/api/**"\n---\nUse FastAPI.\n',
        )
        files = by_path(scan_tree(tmp_path))
        cf = files[".github/instructions/api.instructions.md"]
        assert cf.ecosystem == Ecosystem.COPILOT
        assert cf.mechanism == "instructions"
        assert cf.activation.mode == ActivationMode.PATH
        assert cf.activation.globs == ("src/api/**",)


# ---------------------------------------------------------------------------
# Zed first-match post-pass
# ---------------------------------------------------------------------------


class TestZedFirstMatch:
    def test_only_first_match_keeps_zed_reader(self, tmp_path: Path) -> None:
        write(tmp_path, "AGENTS.md", "Agents guidance.\n")
        write(tmp_path, "CLAUDE.md", "Claude guidance.\n")
        corpus = scan_tree(tmp_path)
        files = by_path(corpus)
        assert "zed" in files["AGENTS.md"].meta["readers"]
        assert "zed" not in files["CLAUDE.md"].meta["readers"]
        assert any("Zed reads only AGENTS.md" in n and "CLAUDE.md" in n for n in corpus.notes)


# ---------------------------------------------------------------------------
# Repo walking and known commands
# ---------------------------------------------------------------------------


class TestRepoHelpers:
    def test_walk_repo_skips_vendored_dirs(self, tmp_path: Path) -> None:
        write(tmp_path, "src/main.py", "print('hi')\n")
        write(tmp_path, "README.md", "hello\n")
        write(tmp_path, "node_modules/pkg/index.js", "module.exports = 1;\n")
        write(tmp_path, ".git/HEAD", "ref: refs/heads/main\n")
        found = walk_repo(tmp_path)
        assert "src/main.py" in found
        assert "README.md" in found
        assert not any(p.startswith(("node_modules/", ".git/")) for p in found)

    def test_discover_known_commands(self, tmp_path: Path) -> None:
        write(tmp_path, "package.json", '{"scripts": {"test": "jest", "build": "webpack"}}\n')
        write(tmp_path, "Makefile", "lint:\n\truff check .\n\ndeploy:\n\t./deploy.sh\n")
        write(
            tmp_path,
            "pyproject.toml",
            '[project]\nname = "x"\n\n[project.scripts]\nmycli = "x.cli:main"\n',
        )
        cmds = discover_known_commands(tmp_path, set(walk_repo(tmp_path)))
        assert {"npm run test", "yarn build", "pnpm run test"} <= cmds
        assert {"make lint", "make deploy"} <= cmds
        assert "mycli" in cmds
