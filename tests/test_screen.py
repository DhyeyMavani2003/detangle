"""Screen-lane tests: nomination parsing, chunking, pipeline integration."""

from __future__ import annotations

import json
from pathlib import Path

from detangle.config import Config
from detangle.lanes.backends import Backend
from detangle.lanes.screen import MAX_UNITS_PER_CALL, _chunks, _parse_nominations
from detangle.pipeline import scan

from .conftest import write_tree


class TestParseNominations:
    def test_valid_array(self):
        raw = json.dumps(
            [
                {"a": 0, "b": 2, "kind": "order", "why": "lint/test order"},
                {"a": 1, "b": 3, "kind": "cross-layer", "why": "x"},
            ]
        )
        out = _parse_nominations(raw, 4)
        assert out == [(0, 2, "order", "lint/test order"), (1, 3, "cross-layer", "x")]

    def test_prose_around_json_tolerated(self):
        raw = 'Here are the pairs:\n[{"a": 0, "b": 1, "kind": "contradiction", "why": "w"}]\nDone.'
        assert len(_parse_nominations(raw, 2)) == 1

    def test_out_of_range_and_self_pairs_dropped(self):
        raw = json.dumps([{"a": 0, "b": 9}, {"a": 1, "b": 1}, {"a": -1, "b": 0}])
        assert _parse_nominations(raw, 3) == []

    def test_garbage_yields_empty(self):
        assert _parse_nominations("no json here", 5) == []
        assert _parse_nominations('{"not": "a list"}', 5) == []
        assert _parse_nominations('[{"a": "x", "b": 1}]', 5) == []

    def test_empty_array(self):
        assert _parse_nominations("[]", 5) == []


class TestChunking:
    def test_small_config_single_chunk(self):
        from detangle.ir import (
            Activation,
            ActivationMode,
            ConfigFile,
            Ecosystem,
            InstructionUnit,
            Layer,
            SourceSpan,
        )

        def unit(i, mode=ActivationMode.ALWAYS):
            cf = ConfigFile(
                path=f"f{i}.md",
                ecosystem=Ecosystem.CLAUDE_CODE,
                layer=Layer.PROJECT,
                tier=20,
                activation=Activation(mode=mode),
                text="",
                mechanism="memory",
            )
            return InstructionUnit(
                text=f"unit {i}",
                normalized=f"unit {i}",
                span=SourceSpan(cf.path, 1, 1),
                file=cf,
                activation=cf.activation,
            )

        units = [unit(i) for i in range(10)]
        assert _chunks(units) == [list(enumerate(units))]

        # large config: always-on units are repeated in every chunk
        many = [
            unit(i, ActivationMode.ALWAYS if i < 5 else ActivationMode.MODEL) for i in range(400)
        ]
        chunks = _chunks(many)
        assert len(chunks) > 1
        always_ids = {i for i, u in enumerate(many) if i < 5}
        for chunk in chunks:
            assert len(chunk) <= MAX_UNITS_PER_CALL + 5
            assert always_ids <= {i for i, _ in chunk}


class _ScriptedScreenBackend(Backend):
    """Screen call returns nominations built from the actual unit listing;
    jury calls return agreeing CONTRADICTORY verdicts quoting real texts."""

    name = "fake"
    model = "scripted"

    def __init__(self):
        self.screen_calls = 0
        self.judge_calls = 0

    def complete(self, system: str, user: str) -> str:
        if "Nominate pairs" in user:
            self.screen_calls += 1
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
        import re as _re

        texts = _re.findall(r'text: "((?:[^"\\]|\\.)*)"', user)
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


def test_screen_pipeline_end_to_end(tmp_path: Path, monkeypatch):
    """--screen: high-recall extraction -> screen nomination -> jury verdict."""
    write_tree(
        tmp_path,
        {
            "CLAUDE.md": (
                "# Workflow\n\n"
                "Run the linter first, then the test suite; commit only after both pass.\n"
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
        },
    )
    backend = _ScriptedScreenBackend()
    monkeypatch.setattr("detangle.lanes.backends.make_backend", lambda cfg, role="jury": backend)
    cfg = Config(root=tmp_path)
    cfg.lane_screen = True
    result = scan(cfg)

    assert cfg.lane_jury is True  # screen implies jury
    assert backend.screen_calls == 1
    assert backend.judge_calls == 2  # both orderings of the nominated pair
    screened = [f for f in result.findings if "screen" in f.lanes]
    assert screened, result.corpus.notes
    f = screened[0]
    assert f.code == "DTC01" and "jury" in f.lanes
    paths = {ev.span.path for ev in f.evidence}
    assert paths == {"CLAUDE.md", ".claude/skills/pre-commit/SKILL.md"}


def test_screen_lane_unavailable_backend_skips_gracefully(tmp_path: Path, monkeypatch):
    from detangle.lanes.backends import JuryError

    def boom(cfg, role="jury"):
        raise JuryError("no backend for tests")

    monkeypatch.setattr("detangle.lanes.backends.make_backend", boom)
    write_tree(tmp_path, {"CLAUDE.md": "# T\n\nNever push to main.\n"})
    cfg = Config(root=tmp_path)
    cfg.lane_screen = True
    result = scan(cfg)  # must not raise
    assert any("skipped" in n or "unavailable" in n for n in result.corpus.notes)
