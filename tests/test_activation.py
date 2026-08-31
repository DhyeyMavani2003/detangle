"""Unit tests for the co-activation engine and precedence model (detangle.activation)."""

from __future__ import annotations

from detangle.activation import co_activation, precedence, scope_relation
from detangle.ir import (
    Activation,
    ActivationMode,
    CoActiveClass,
    ConfigFile,
    Ecosystem,
    InstructionUnit,
    Layer,
    PrecedenceKind,
    SourceSpan,
)


def make_unit(
    text: str = "Always run the tests.",
    *,
    path: str = "CLAUDE.md",
    ecosystem: Ecosystem = Ecosystem.CLAUDE_CODE,
    layer: Layer = Layer.PROJECT,
    tier: int = 20,
    mechanism: str = "memory",
    tool: str = "claude-code",
    load_order: int = 0,
    mode: ActivationMode = ActivationMode.ALWAYS,
    globs: tuple[str, ...] = (),
    description: str = "",
    readers: tuple[str, ...] = ("claude-code",),
    context_scope: str = "",
) -> InstructionUnit:
    """Hand-build a ConfigFile + InstructionUnit pair for activation tests."""
    activation = Activation(mode=mode, globs=globs, description=description)
    cf = ConfigFile(
        path=path,
        ecosystem=ecosystem,
        layer=layer,
        tier=tier,
        activation=activation,
        text=text,
        mechanism=mechanism,
        tool=tool,
        load_order=load_order,
    )
    cf.meta["readers"] = readers
    if context_scope:
        cf.meta["context_scope"] = context_scope
    return InstructionUnit(
        text=text,
        normalized=text,
        span=SourceSpan(path=path, start_line=1, end_line=1),
        file=cf,
        activation=activation,
    )


# ---------------------------------------------------------------------------
# co_activation
# ---------------------------------------------------------------------------


class TestCoActivation:
    def test_always_always(self) -> None:
        a = make_unit(path="CLAUDE.md")
        b = make_unit(path=".claude/CLAUDE.md")
        cls, account = co_activation(a, b)
        assert cls == CoActiveClass.ALWAYS_ALWAYS
        assert "launch" in account

    def test_always_conditional(self) -> None:
        a = make_unit(path="CLAUDE.md")
        b = make_unit(path="src/CLAUDE.md", mode=ActivationMode.PATH, globs=("src/**",))
        cls, account = co_activation(a, b)
        assert cls == CoActiveClass.ALWAYS_CONDITIONAL
        assert "src/**" in account

    def test_path_path_overlapping(self) -> None:
        a = make_unit(path="a.md", mode=ActivationMode.PATH, globs=("src/**",))
        b = make_unit(path="b.md", mode=ActivationMode.PATH, globs=("src/api/**",))
        cls, account = co_activation(a, b)
        assert cls == CoActiveClass.CONDITIONAL_OVERLAPPING
        assert "intersect" in account

    def test_path_path_disjoint(self) -> None:
        a = make_unit(path="a.md", mode=ActivationMode.PATH, globs=("src/**",))
        b = make_unit(path="b.md", mode=ActivationMode.PATH, globs=("docs/**",))
        cls, account = co_activation(a, b)
        assert cls == CoActiveClass.MUTUALLY_EXCLUSIVE
        assert "disjoint" in account

    def test_different_subagent_scopes_are_mutually_exclusive(self) -> None:
        a = make_unit(
            path=".claude/agents/alpha.md",
            mechanism="subagent",
            mode=ActivationMode.MODEL,
            description="Alpha agent",
            context_scope="subagent:alpha",
        )
        b = make_unit(
            path=".claude/agents/beta.md",
            mechanism="subagent",
            mode=ActivationMode.MODEL,
            description="Beta agent",
            context_scope="subagent:beta",
        )
        cls, account = co_activation(a, b)
        assert cls == CoActiveClass.MUTUALLY_EXCLUSIVE
        assert "separate contexts" in account

    def test_subagent_is_isolated_from_main_context_skills(self) -> None:
        a = make_unit(
            path=".claude/agents/alpha.md",
            mechanism="subagent",
            mode=ActivationMode.MODEL,
            context_scope="subagent:alpha",
        )
        b = make_unit(
            path=".claude/skills/deploy/SKILL.md",
            mechanism="skill",
            mode=ActivationMode.MODEL,
            description="Deploys the app",
        )
        cls, account = co_activation(a, b)
        assert cls == CoActiveClass.MUTUALLY_EXCLUSIVE
        assert "isolated" in account

    def test_subagent_still_co_activates_with_memory(self) -> None:
        a = make_unit(
            path=".claude/agents/alpha.md",
            mechanism="subagent",
            mode=ActivationMode.MODEL,
            context_scope="subagent:alpha",
        )
        b = make_unit(path="CLAUDE.md", mechanism="memory")
        cls, _ = co_activation(a, b)
        assert cls == CoActiveClass.ALWAYS_CONDITIONAL

    def test_no_common_reader_is_cross_tool_only(self) -> None:
        a = make_unit(path="CLAUDE.md", readers=("claude-code",))
        b = make_unit(
            path=".cursor/rules/x.mdc",
            ecosystem=Ecosystem.CURSOR,
            mechanism="cursor-rule",
            readers=("cursor",),
        )
        cls, account = co_activation(a, b)
        assert cls == CoActiveClass.CROSS_TOOL_ONLY
        assert "no tool reads both" in account

    def test_model_model_is_conditional_overlapping(self) -> None:
        a = make_unit(
            path=".claude/skills/a/SKILL.md",
            mechanism="skill",
            mode=ActivationMode.MODEL,
            description="Deploy the app to production",
        )
        b = make_unit(
            path=".claude/skills/b/SKILL.md",
            mechanism="skill",
            mode=ActivationMode.MODEL,
            description="Release the app to production",
        )
        cls, account = co_activation(a, b)
        assert cls == CoActiveClass.CONDITIONAL_OVERLAPPING
        assert "description-triggered" in account

    def test_user_invoked_can_co_occur_with_path(self) -> None:
        a = make_unit(
            path=".claude/commands/ship.md", mechanism="command", mode=ActivationMode.USER
        )
        b = make_unit(path="b.md", mode=ActivationMode.PATH, globs=("src/**",))
        cls, account = co_activation(a, b)
        assert cls == CoActiveClass.CONDITIONAL_OVERLAPPING
        assert "user-invoked" in account


# ---------------------------------------------------------------------------
# precedence
# ---------------------------------------------------------------------------


class TestPrecedence:
    def test_claude_memory_is_ambiguous_quoting_arbitrarily(self) -> None:
        a = make_unit(path="CLAUDE.md")
        b = make_unit(path=".claude/CLAUDE.md")
        rel = precedence(a, b)
        assert rel.kind == PrecedenceKind.AMBIGUOUS
        assert "arbitrarily" in rel.account
        assert rel.higher is None

    def test_rules_different_tier_positional_project_wins(self) -> None:
        user = make_unit(path=".claude/rules/user.md", mechanism="rules", tier=10)
        project = make_unit(path=".claude/rules/project.md", mechanism="rules", tier=20)
        rel = precedence(user, project)
        assert rel.kind == PrecedenceKind.POSITIONAL
        # project rules load after user rules, so the project rule wins
        assert rel.higher is project
        # symmetric regardless of argument order
        assert precedence(project, user).higher is project

    def test_rules_same_tier_ambiguous(self) -> None:
        a = make_unit(path=".claude/rules/a.md", mechanism="rules", tier=20)
        b = make_unit(path=".claude/rules/b.md", mechanism="rules", tier=20)
        rel = precedence(a, b)
        assert rel.kind == PrecedenceKind.AMBIGUOUS

    def test_skills_same_tier_ambiguous(self) -> None:
        a = make_unit(path=".claude/skills/a/SKILL.md", mechanism="skill", tier=20)
        b = make_unit(path=".claude/skills/b/SKILL.md", mechanism="skill", tier=20)
        rel = precedence(a, b)
        assert rel.kind == PrecedenceKind.AMBIGUOUS
        assert "arbitration" in rel.account

    def test_skills_different_tier_resolved_by_name_shadowing(self) -> None:
        ent = make_unit(path="enterprise/SKILL.md", mechanism="skill", tier=0)
        proj = make_unit(path=".claude/skills/x/SKILL.md", mechanism="skill", tier=20)
        rel = precedence(ent, proj)
        assert rel.kind == PrecedenceKind.RESOLVED
        assert rel.higher is ent

    def test_agents_md_different_load_order_positional_later_wins(self) -> None:
        root = make_unit(
            path="AGENTS.md",
            ecosystem=Ecosystem.AGENTS_MD,
            tool="agents-md",
            readers=("codex",),
            load_order=0,
        )
        sub = make_unit(
            path="app/AGENTS.md",
            ecosystem=Ecosystem.AGENTS_MD,
            layer=Layer.SUBDIR,
            tier=30,
            tool="agents-md",
            readers=("codex",),
            load_order=1,
        )
        rel = precedence(root, sub)
        assert rel.kind == PrecedenceKind.POSITIONAL
        assert rel.higher is sub
        assert "later" in rel.account

    def test_agents_md_same_load_order_ambiguous(self) -> None:
        a = make_unit(path="a/AGENTS.md", ecosystem=Ecosystem.AGENTS_MD, load_order=1)
        b = make_unit(path="b/AGENTS.md", ecosystem=Ecosystem.AGENTS_MD, load_order=1)
        assert precedence(a, b).kind == PrecedenceKind.AMBIGUOUS

    def test_cross_ecosystem_undocumented(self) -> None:
        a = make_unit(path="CLAUDE.md")
        b = make_unit(
            path=".cursor/rules/x.mdc",
            ecosystem=Ecosystem.CURSOR,
            mechanism="cursor-rule",
            readers=("cursor",),
        )
        rel = precedence(a, b)
        assert rel.kind == PrecedenceKind.UNDOCUMENTED
        assert "different config surfaces" in rel.account

    def test_cross_mechanism_same_ecosystem_undocumented(self) -> None:
        a = make_unit(path="CLAUDE.md", mechanism="memory")
        b = make_unit(path=".claude/skills/x/SKILL.md", mechanism="skill")
        rel = precedence(a, b)
        assert rel.kind == PrecedenceKind.UNDOCUMENTED
        assert "cross-mechanism" in rel.account

    def test_same_file_ambiguous(self) -> None:
        a = make_unit(path="CLAUDE.md")
        b = make_unit(text="Never push to main.", path="CLAUDE.md")
        rel = precedence(a, b)
        assert rel.kind == PrecedenceKind.AMBIGUOUS
        assert "same file" in rel.account


# ---------------------------------------------------------------------------
# scope_relation
# ---------------------------------------------------------------------------


class TestScopeRelation:
    def test_equal_globs(self) -> None:
        a = make_unit(path="a.md", mode=ActivationMode.PATH, globs=("src/**",))
        b = make_unit(path="b.md", mode=ActivationMode.PATH, globs=("src/**",))
        assert scope_relation(a, b) == "equal"

    def test_always_always_equal(self) -> None:
        a = make_unit(path="a.md")
        b = make_unit(path="b.md")
        assert scope_relation(a, b) == "equal"

    def test_subset_both_directions(self) -> None:
        narrow = make_unit(path="a.md", mode=ActivationMode.PATH, globs=("src/api/**",))
        wide = make_unit(path="b.md", mode=ActivationMode.PATH, globs=("src/**",))
        assert scope_relation(narrow, wide) == "a-subset-of-b"
        assert scope_relation(wide, narrow) == "b-subset-of-a"

    def test_path_subset_of_always(self) -> None:
        always = make_unit(path="a.md")
        scoped = make_unit(path="b.md", mode=ActivationMode.PATH, globs=("src/**",))
        assert scope_relation(always, scoped) == "b-subset-of-a"

    def test_overlap(self) -> None:
        a = make_unit(path="a.md", mode=ActivationMode.PATH, globs=("src/**",))
        b = make_unit(path="b.md", mode=ActivationMode.PATH, globs=("**/*.py",))
        assert scope_relation(a, b) == "overlap"

    def test_disjoint(self) -> None:
        a = make_unit(path="a.md", mode=ActivationMode.PATH, globs=("src/**",))
        b = make_unit(path="b.md", mode=ActivationMode.PATH, globs=("docs/**",))
        assert scope_relation(a, b) == "disjoint"

    def test_model_scope_is_unknown(self) -> None:
        a = make_unit(path="a.md", mode=ActivationMode.MODEL, description="Deploys")
        b = make_unit(path="b.md", mode=ActivationMode.PATH, globs=("src/**",))
        assert scope_relation(a, b) == "unknown"
        assert scope_relation(b, a) == "unknown"
