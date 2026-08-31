"""Deep-mode screen tests: multi-sweep nomination, cross-sweep dedup, caching."""

from __future__ import annotations

import json
import re
from pathlib import Path

from detangle.config import Config
from detangle.lanes.backends import Backend
from detangle.lanes.screen import FOCUSED_KINDS, SCREEN_SYSTEM_PROMPT, _sweep_prompts
from detangle.pipeline import scan

from .conftest import write_tree

# a small single-chunk tree with one real order conflict (main file vs skill)
TREE = {
    "CLAUDE.md": (
        "# Workflow\n\nRun the linter first, then the test suite; commit only after both pass.\n"
    ),
    ".claude/skills/pre-commit/SKILL.md": (
        "---\n"
        "name: pre-commit\n"
        "description: Use before committing changes.\n"
        "---\n"
        "# Checks\n\n"
        "Start with the test suite so failures surface early, and save linting "
        "for the very end once tests are green.\n"
    ),
}


class _ScriptedDeepBackend(Backend):
    """Records each screen call's system prompt. Nominates the lint-order pair
    only on sweeps whose system prompt contains one of ``nominate_on`` (by
    default the focused 'order' sweep), returning [] for every other sweep;
    jury calls get agreeing CONTRADICTORY verdicts quoting real texts."""

    name = "fake"
    model = "scripted"

    def __init__(self, nominate_on: tuple[str, ...] = ("'order'",)):
        self.nominate_on = nominate_on
        self.screen_calls = 0
        self.judge_calls = 0
        self.screen_systems: list[str] = []

    def complete(self, system: str, user: str) -> str:
        if "Nominate pairs" in user:
            self.screen_calls += 1
            self.screen_systems.append(system)
            if not any(marker in system for marker in self.nominate_on):
                return "[]"
            # nominate the lint-order pair by finding the two unit indexes
            lines = [ln for ln in user.split("\n") if ln.startswith("[")]
            a = b = None
            for ln in lines:
                idx = int(ln[1 : ln.index("]")])
                if "linter first" in ln:
                    a = idx
                if "Start with the test suite" in ln:
                    b = idx
            if a is None or b is None:
                return "[]"
            return json.dumps([{"a": a, "b": b, "kind": "order", "why": "step order"}])
        self.judge_calls += 1
        # extract the two instruction texts to quote as evidence
        texts = re.findall(r'text: "((?:[^"\\]|\\.)*)"', user)
        ev_a = json.loads(f'"{texts[0]}"') if texts else ""
        ev_b = json.loads(f'"{texts[1]}"') if len(texts) > 1 else ""
        return json.dumps(
            {
                "overlap_condition": "when the pre-commit skill fires",
                "evidence_a": ev_a,
                "evidence_b": ev_b,
                "reasoning_summary": "opposite step order",
                "verdict": "CONTRADICTORY",
                "conflict_type": "process",
                "resolution_hint": "pick one order",
                "confidence": 0.9,
            }
        )


def _install(monkeypatch, backend: Backend) -> None:
    monkeypatch.setattr("detangle.lanes.backends.make_backend", lambda cfg, role="jury": backend)
    # deep mode may switch the NLI lane on in the pipeline; keep these tests
    # hermetic (no model load) — deep's screen behavior is what's under test
    monkeypatch.setattr("detangle.lanes.nli.run_nli_lane", lambda cfg, ctx, findings: findings)


def _deep_config(root: Path, cache_dir: Path, deep: bool = True) -> Config:
    cfg = Config(root=root)
    cfg.lane_screen = True
    cfg.deep = deep
    cfg.cache_dir = cache_dir
    return cfg


class TestSweepPrompts:
    def test_shallow_is_generic_only(self):
        assert _sweep_prompts(False) == [("generic", SCREEN_SYSTEM_PROMPT)]

    def test_deep_adds_one_focused_sweep_per_kind(self):
        sweeps = _sweep_prompts(True)
        assert [label for label, _ in sweeps] == ["generic"] + [f"focus:{k}" for k in FOCUSED_KINDS]
        for label, prompt in sweeps[1:]:
            kind = label.removeprefix("focus:")
            assert prompt.startswith(SCREEN_SYSTEM_PROMPT)  # JSON contract intact
            assert "THIS SWEEP IS FOCUSED" in prompt
            assert f"'{kind}'" in prompt
        # every sweep prompt is distinct -> distinct cache identities
        assert len({p for _, p in sweeps}) == len(sweeps)


def test_deep_scan_issues_one_call_per_sweep(tmp_path: Path, monkeypatch):
    write_tree(tmp_path, TREE)
    backend = _ScriptedDeepBackend()
    _install(monkeypatch, backend)
    scan(_deep_config(tmp_path, tmp_path / "cache-deep"))
    assert backend.screen_calls == 1 + len(FOCUSED_KINDS) == 10
    focused = [s for s in backend.screen_systems if "THIS SWEEP IS FOCUSED" in s]
    assert len(focused) == len(FOCUSED_KINDS)

    shallow = _ScriptedDeepBackend()
    _install(monkeypatch, shallow)
    scan(_deep_config(tmp_path, tmp_path / "cache-shallow", deep=False))
    assert shallow.screen_calls == 1


def test_pair_nominated_by_two_sweeps_reaches_jury_once(tmp_path: Path, monkeypatch):
    write_tree(tmp_path, TREE)
    backend = _ScriptedDeepBackend(nominate_on=("'order'", "'contradiction'"))
    _install(monkeypatch, backend)
    result = scan(_deep_config(tmp_path, tmp_path / "cache"))
    # two sweeps nominated the same pair; dedup sends it to the jury once
    assert backend.judge_calls == 2  # one pair, both orderings
    screened = [f for f in result.findings if "screen" in f.lanes]
    assert len(screened) == 1
    assert any(
        "2 nomination(s)" in n and "10 sweep(s)" in n and "1 pair(s)" in n
        for n in result.corpus.notes
    ), result.corpus.notes


def test_deep_rescan_of_unchanged_tree_makes_zero_screen_calls(tmp_path: Path, monkeypatch):
    write_tree(tmp_path, TREE)
    shared_cache = tmp_path / "shared-cache"
    first = _ScriptedDeepBackend()
    _install(monkeypatch, first)
    result1 = scan(_deep_config(tmp_path, shared_cache))
    assert first.screen_calls == 10

    second = _ScriptedDeepBackend()
    _install(monkeypatch, second)
    result2 = scan(_deep_config(tmp_path, shared_cache))
    assert second.screen_calls == 0
    assert second.judge_calls == 0  # jury verdicts are cached too
    # cached nominations still reach the jury and reproduce the finding
    codes = lambda r: sorted(f.code for f in r.findings if "screen" in f.lanes)  # noqa: E731
    assert codes(result2) == codes(result1) != []


def test_focused_sweep_finding_carries_screen_lane(tmp_path: Path, monkeypatch):
    write_tree(tmp_path, TREE)
    backend = _ScriptedDeepBackend()  # nominates ONLY on the focused order sweep
    _install(monkeypatch, backend)
    result = scan(_deep_config(tmp_path, tmp_path / "cache"))
    screened = [f for f in result.findings if "screen" in f.lanes]
    assert screened, result.corpus.notes
    f = screened[0]
    assert f.code == "DTC01" and "jury" in f.lanes
    assert {ev.span.path for ev in f.evidence} == {
        "CLAUDE.md",
        ".claude/skills/pre-commit/SKILL.md",
    }
