"""TypeSafe lane: end-to-end through the pipeline against a scripted local
HTTP server that speaks the System One evaluation contract. No API key or
network needed in CI."""

from __future__ import annotations

import json
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from detangle.config import Config, load_config
from detangle.lanes.typesafe import RELATION_CRITERIA, _conflict_mass, _sanitize, _verdict
from detangle.pipeline import scan

from .conftest import write_tree

TREE = {
    "CLAUDE.md": (
        "# Workflow\n\n"
        "Run the linter first, then the test suite; commit only after both pass.\n\n"
        "Keep the changelog current.\n"
    ),
    ".claude/skills/pre-commit/SKILL.md": (
        "---\nname: pre-commit\ndescription: Use before committing changes.\n---\n"
        "# Checks\n\n"
        "Start with the test suite so failures surface early, and save linting for "
        "the very end once tests are green.\n"
    ),
}


class _Scripted:
    """Answers every relation question: the lint/test pair is contradictory
    (both orderings), everything else distinct. ``batched`` is the lint/test
    answer inside a shared state, ``alone`` the answer when the pair has the
    request to itself (the lane's solo re-ask); None means "same as batched"."""

    def __init__(self):
        self.calls = 0
        self.questions_seen = 0
        self.state_sizes: list[int] = []
        self.fail_first = False
        # one-shot misbehaviour for the NEXT request: "429", "500", "drop"
        # (close without a response), "list" (a JSON list body), "strings"
        # (per-question error strings), "poison" (string-valued probabilities)
        self.once: str | None = None
        self.self_questions = 0  # a unit asked about itself: must never happen
        self.batched = {"contradictory": 0.92, "conditional_conflict": 0.05, "distinct": 0.03}
        self.alone: dict | None = None

    def answer(self, body: dict) -> dict:
        self.calls += 1
        self.questions_seen += len(body["questions"])
        units = body["state"]["units"]
        self.state_sizes.append(len(units))
        answers = {}
        for qid, q in body["questions"].items():
            assert q["type"] == "choice" and q["criteria"] == RELATION_CRITERIA
            # the question carries the pair's co-activation account and precedence
            assert set(q["instructions"]["co_activation"]) == {"class", "account", "precedence"}
            _, a, b = qid.split("_", 2)
            self.self_questions += a == b
            texts = units[a]["text"] + " " + units[b]["text"]
            if "linter first" in texts and "Start with the test suite" in texts:
                probs = dict.fromkeys(RELATION_CRITERIA, 0.0)
                probs.update(self.alone if (len(units) == 2 and self.alone) else self.batched)
            else:
                probs = dict.fromkeys(RELATION_CRITERIA, 0.0)
                probs["distinct"] = 0.97
                probs["redundant"] = 0.03
            answers[qid] = {
                "type": "choice",
                "choice": max(probs, key=probs.get),
                "probabilities": probs,
                "confidence": 0.9,
            }
        mode, self.once = self.once, None
        if mode == "strings":
            answers = dict.fromkeys(answers, "error: rate limited")
        elif mode == "poison":
            for ans in answers.values():
                ans["probabilities"] = dict.fromkeys(ans["probabilities"], "n/a")
        return {
            "model": "jev-test",
            "answers": answers,
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }


@pytest.fixture
def server():
    scripted = _Scripted()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.headers.get("Authorization") != "Bearer test-key":
                self.send_response(401)
                self.end_headers()
                return
            if scripted.fail_first:
                scripted.fail_first = False
                self.send_response(429)
                self.end_headers()
                return
            n = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(n))
            if scripted.once in ("429", "500"):
                code, scripted.once = int(scripted.once), None
                self.send_response(code)
                self.end_headers()
                return
            if scripted.once == "drop":
                scripted.once = None
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            if scripted.once == "list":
                scripted.once = None
                payload = b"[]"
            else:
                payload = json.dumps(scripted.answer(body)).encode()
            out = payload
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):  # quiet
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield scripted, f"http://127.0.0.1:{httpd.server_port}/v1/systemone"
    httpd.shutdown()


def _cfg(tmp_path: Path, endpoint: str, **over) -> Config:
    cfg = Config(root=tmp_path)
    cfg.lane_typesafe = True
    cfg.typesafe_endpoint = endpoint
    cfg.typesafe_api_key_env = "TS_TEST_KEY"
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


def test_lane_emits_batched_swapped_verdicts_and_caches(tmp_path: Path, server, monkeypatch):
    scripted, endpoint = server
    monkeypatch.setenv("TS_TEST_KEY", "test-key")
    write_tree(tmp_path, TREE)

    r1 = scan(_cfg(tmp_path, endpoint))
    ts = [f for f in r1.findings if "typesafe" in f.lanes]
    assert len(ts) == 1, [f.message for f in r1.findings]
    f = ts[0]
    # memory vs skill with no documented precedence: the structural overlay
    # routes it as a cross-layer collision, exactly as the deterministic router does
    assert f.code == "DTP04" and f.severity.label == "warning"
    assert "contradictory" in f.message
    assert {ev.span.path for ev in f.evidence} == set(TREE)
    # all pairs of a small config batch into one request; the one pair that
    # scored into the band or above is then re-asked alone (two-unit state)
    assert scripted.calls == 2
    assert scripted.state_sizes[0] > 2 and scripted.state_sizes[1] == 2
    # every pair asked in both orderings inside the batched request
    assert scripted.questions_seen % 2 == 0 and scripted.questions_seen >= 4
    assert any("typesafe lane: judged" in n and "1 re-judged alone" in n for n in r1.corpus.notes)

    # second scan: verdicts cached -> zero calls, identical findings
    calls_before = scripted.calls
    r2 = scan(_cfg(tmp_path, endpoint))
    assert scripted.calls == calls_before
    assert [x.fingerprint for x in r2.findings] == [x.fingerprint for x in r1.findings]


def test_missing_key_skips_gracefully(tmp_path: Path, server, monkeypatch):
    _, endpoint = server
    monkeypatch.delenv("TS_TEST_KEY", raising=False)
    write_tree(tmp_path, TREE)
    r = scan(_cfg(tmp_path, endpoint))
    assert not [f for f in r.findings if "typesafe" in f.lanes]
    assert any("lane skipped" in n for n in r.corpus.notes)


def test_rate_limit_is_retried(tmp_path: Path, server, monkeypatch):
    scripted, endpoint = server
    scripted.fail_first = True
    monkeypatch.setenv("TS_TEST_KEY", "test-key")
    monkeypatch.setattr("detangle.lanes.typesafe.time.sleep", lambda s: None)
    write_tree(tmp_path, TREE)
    r = scan(_cfg(tmp_path, endpoint))
    assert [f for f in r.findings if "typesafe" in f.lanes]


def test_uncertain_band_goes_to_jury_channel(tmp_path: Path, server, monkeypatch):
    """A pair below tau but above uncertain_low is handed to the jury through
    the jury queue; confident verdicts never are."""
    scripted, endpoint = server
    monkeypatch.setenv("TS_TEST_KEY", "test-key")
    write_tree(tmp_path, TREE)
    captured = {}

    def fake_jury(cfg, ctx, findings):
        captured["band"] = list(ctx.jury_queue or [])
        return findings

    monkeypatch.setattr("detangle.lanes.jury.run_jury_lane", fake_jury)
    # tau above the scripted 0.97 conflict mass -> the pair is "uncertain"
    cfg = _cfg(tmp_path, endpoint, lane_jury=True, typesafe_tau=0.99, typesafe_strong=0.995)
    r = scan(cfg)
    assert not [f for f in r.findings if "typesafe" in f.lanes]
    assert len(captured["band"]) == 1
    pair, mass = captured["band"][0]
    assert mass == pytest.approx(0.97)


def test_solo_rejudge_clears_but_needs_both_passes_to_emit(tmp_path: Path, server, monkeypatch):
    """The second pass re-asks band-or-above pairs alone: the solo answer
    clears batched noise, a promotion needs the batched pass's agreement, and
    the weaker of the two passes sets the mass; ``rejudge = false`` keeps the
    batched verdict."""
    scripted, endpoint = server
    monkeypatch.setenv("TS_TEST_KEY", "test-key")
    captured = {}

    def fake_jury(cfg, ctx, findings):
        captured["band"] = list(ctx.jury_queue or [])
        return findings

    monkeypatch.setattr("detangle.lanes.jury.run_jury_lane", fake_jury)

    # batched noise: confident in the shared state, distinct alone -> nothing
    # emitted and nothing handed on
    write_tree(tmp_path / "noise", TREE)
    scripted.alone = {"distinct": 0.97, "redundant": 0.03}
    r = scan(_cfg(tmp_path / "noise", endpoint, lane_jury=True))
    assert not [f for f in r.findings if "typesafe" in f.lanes]
    assert captured["band"] == []
    assert scripted.calls == 2 and scripted.state_sizes[-1] == 2

    # batched near-miss, confident alone: a promotion the batched pass did not
    # support is NOT emitted — it stays in the band for the jury, scored by
    # the solo verdict
    write_tree(tmp_path / "promote", TREE)
    scripted.batched = {"contradictory": 0.45, "distinct": 0.55}
    scripted.alone = {"contradictory": 0.92, "conditional_conflict": 0.05, "distinct": 0.03}
    r = scan(_cfg(tmp_path / "promote", endpoint, lane_jury=True))
    assert not [f for f in r.findings if "typesafe" in f.lanes]
    assert len(captured["band"]) == 1 and captured["band"][0][1] == pytest.approx(0.97)

    # both passes see the conflict, the solo pass less strongly: emitted at the
    # weaker mass (advisory, below `strong`)
    write_tree(tmp_path / "soften", TREE)
    scripted.batched = {"contradictory": 0.92, "conditional_conflict": 0.05, "distinct": 0.03}
    scripted.alone = {"contradictory": 0.75, "distinct": 0.25}
    r = scan(_cfg(tmp_path / "soften", endpoint, lane_jury=True))
    ts = [f for f in r.findings if "typesafe" in f.lanes]
    assert len(ts) == 1 and ts[0].confidence == pytest.approx(0.75)
    assert ts[0].severity.label == "advisory" and captured["band"] == []

    # rejudge off: one batched call, the band verdict stands and goes to the jury
    write_tree(tmp_path / "off", TREE)
    scripted.batched = {"contradictory": 0.45, "distinct": 0.55}
    calls_before = scripted.calls
    r = scan(_cfg(tmp_path / "off", endpoint, lane_jury=True, typesafe_rejudge=False))
    assert scripted.calls == calls_before + 1
    assert not [f for f in r.findings if "typesafe" in f.lanes]
    assert len(captured["band"]) == 1 and captured["band"][0][1] == pytest.approx(0.45)
    assert any("0 re-judged alone" in n for n in r.corpus.notes)


def test_malformed_responses_end_the_lane_cleanly_and_are_never_cached(
    tmp_path: Path, server, monkeypatch
):
    """A JSON list body, per-question error strings and non-numeric
    probabilities all end the lane with a note (no crash, no findings), hand
    nothing to the jury — which then keeps its own candidate ranking — and
    cache nothing, so the next healthy scan judges every pair."""
    scripted, endpoint = server
    monkeypatch.setenv("TS_TEST_KEY", "test-key")
    captured = {}

    def fake_jury(cfg, ctx, findings):
        captured["band"] = "untouched" if ctx.jury_queue is None else ctx.jury_queue
        return findings

    monkeypatch.setattr("detangle.lanes.jury.run_jury_lane", fake_jury)
    write_tree(tmp_path, TREE)
    for mode in ("list", "strings", "poison"):
        scripted.once = mode
        r = scan(_cfg(tmp_path, endpoint, lane_jury=True))
        assert not [f for f in r.findings if "typesafe" in f.lanes], mode
        assert any("lane incomplete" in n for n in r.corpus.notes), (mode, r.corpus.notes)
        assert not any("handed to the jury" in n for n in r.corpus.notes), mode
        assert captured["band"] == "untouched", mode
    r = scan(_cfg(tmp_path, endpoint, lane_jury=True))
    assert len([f for f in r.findings if "typesafe" in f.lanes]) == 1
    assert captured["band"] == []


def test_sanitize_rejects_poisoned_records():
    good = {"probs": {"distinct": 1.0}, "probs_swapped": {"distinct": "1.0"}, "batch": 3}
    assert _sanitize(good) == {
        "probs": {"distinct": 1.0},
        "probs_swapped": {"distinct": 1.0},
        "batch": 3,
    }
    assert _sanitize({"probs": {"distinct": "n/a"}, "probs_swapped": {"distinct": 1.0}}) is None
    assert _sanitize({"probs": {"distinct": 1.0}, "probs_swapped": None}) is None
    assert _sanitize({"probs": {"distinct": 7}, "probs_swapped": {"distinct": 1.0}}) is None
    assert _sanitize({"probs": {}, "probs_swapped": {"distinct": 1.0}}) is None
    assert _sanitize("garbage") is None


@pytest.mark.parametrize("mode", ["drop", "500", "429"])
def test_transient_failures_are_retried(tmp_path: Path, server, monkeypatch, mode):
    """A dropped connection and a 5xx are retried with backoff like a 429."""
    scripted, endpoint = server
    scripted.once = mode
    monkeypatch.setenv("TS_TEST_KEY", "test-key")
    monkeypatch.setattr("detangle.lanes.typesafe.time.sleep", lambda s: None)
    write_tree(tmp_path, TREE)
    r = scan(_cfg(tmp_path, endpoint))
    assert [f for f in r.findings if "typesafe" in f.lanes]
    assert not any("lane incomplete" in n for n in r.corpus.notes)


DUP_TREE = {
    "CLAUDE.md": (
        "# Workflow\n\n"
        "Run the linter first, then the test suite; commit only after both pass.\n\n"
        "Keep the changelog current.\n\n"
        "Keep the changelog current.\n"
    ),
    ".claude/skills/pre-commit/SKILL.md": TREE[".claude/skills/pre-commit/SKILL.md"],
}


def test_verbatim_duplicate_units_do_not_block_completion(tmp_path: Path, server, monkeypatch):
    """A sentence repeated verbatim in one file shares its uid: the copies are
    never paired with each other, each distinct pair key is judged once, and
    the lane still completes — so the baseline may stamp its findings missing."""
    scripted, endpoint = server
    monkeypatch.setenv("TS_TEST_KEY", "test-key")
    write_tree(tmp_path, DUP_TREE)
    r = scan(_cfg(tmp_path, endpoint))
    note = next(n for n in r.corpus.notes if n.startswith("typesafe lane: judged"))
    m = re.search(r"judged (\d+) of (\d+)", note)
    assert m and m.group(1) == m.group(2), note
    assert "lane incomplete" not in note
    assert scripted.self_questions == 0
    assert [f for f in r.findings if "typesafe" in f.lanes]


def test_pair_cap_marks_the_lane_incomplete(tmp_path: Path, server, monkeypatch):
    scripted, endpoint = server
    monkeypatch.setenv("TS_TEST_KEY", "test-key")
    write_tree(tmp_path / "full", TREE)
    r = scan(_cfg(tmp_path / "full", endpoint))
    note = next(n for n in r.corpus.notes if n.startswith("typesafe lane: judged"))
    n_pairs = int(re.search(r"judged (\d+) of (\d+)", note).group(2))
    assert n_pairs > 1
    # a cap equal to the pair count truncates nothing
    write_tree(tmp_path / "exact", TREE)
    r = scan(_cfg(tmp_path / "exact", endpoint, typesafe_max_pairs=n_pairs))
    assert not any("pair cap" in n or "lane incomplete" in n for n in r.corpus.notes)
    # a smaller cap does, and an incomplete lane must not count as ran
    write_tree(tmp_path / "capped", TREE)
    r = scan(_cfg(tmp_path / "capped", endpoint, typesafe_max_pairs=1))
    assert any("pair cap reached" in n for n in r.corpus.notes)
    assert any("lane incomplete" in n for n in r.corpus.notes)


def test_verdict_uses_min_mass_and_mean_class_and_detects_redundancy():
    base = dict.fromkeys(RELATION_CRITERIA, 0.0)
    # orderings disagree on flavor: the MEAN distribution picks the class,
    # the MIN conflict mass gates emission
    fwd = dict(base, contradictory=0.8, conditional_conflict=0.15, distinct=0.05)
    rev = dict(base, contradictory=0.2, conditional_conflict=0.7, distinct=0.1)
    code, mass, cls = _verdict({"probs": fwd, "probs_swapped": rev})
    assert code == "DTC01" and cls == "contradictory"  # mean 0.5 vs 0.425
    assert mass == pytest.approx(min(_conflict_mass(fwd), _conflict_mass(rev)))
    num = dict(base, numeric_limit_conflict=0.9, distinct=0.1)
    assert _verdict({"probs": num, "probs_swapped": num})[0] == "DTC03"
    order = dict(base, order_conflict=0.95, distinct=0.05)
    assert _verdict({"probs": order, "probs_swapped": order})[0] == "DTC02"
    soft = dict(base, goal_tension=0.9, distinct=0.1)
    assert _verdict({"probs": soft, "probs_swapped": soft})[0] == "DTC08"
    red = dict(base, redundant=0.85, distinct=0.15)
    assert _verdict({"probs": red, "probs_swapped": red})[0] == "DTR01"
    weak_red = dict(base, redundant=0.65, distinct=0.35)  # below the 0.7 bar
    assert _verdict({"probs": weak_red, "probs_swapped": weak_red})[0] != "DTR01"
    # a solo pass alone cannot call a pair redundant that the batched pass saw
    # as a conflict: both passes must agree, as for conflicts
    solo_red = {"probs": red, "probs_swapped": red, "batched_mass": 0.45, "batched_redundant": 0.0}
    assert _verdict(solo_red)[0] != "DTR01"


def test_config_parsing_and_validation(tmp_path: Path):
    (tmp_path / ".detangle.toml").write_text(
        "[detangle.lanes]\ntypesafe = true\n"
        "[detangle.typesafe]\nmodel = 'jev-2'\npairs = 'candidates'\ntau = 0.6\n"
    )
    cfg = load_config(tmp_path)
    assert cfg.lane_typesafe and cfg.typesafe_model == "jev-2"
    assert cfg.typesafe_pairs == "candidates" and cfg.typesafe_tau == 0.6
    assert cfg.typesafe_rejudge is True
    (tmp_path / ".detangle.toml").write_text("[detangle.typesafe]\nrejudge = false\n")
    assert load_config(tmp_path).typesafe_rejudge is False
    (tmp_path / ".detangle.toml").write_text("[detangle.typesafe]\ntau = 0.95\nstrong = 0.5\n")
    with pytest.raises(Exception, match="thresholds"):
        load_config(tmp_path)
    (tmp_path / ".detangle.toml").write_text("[detangle.typesafe]\npairs = 'some'\n")
    with pytest.raises(Exception, match="pairs"):
        load_config(tmp_path)
