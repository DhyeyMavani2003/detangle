"""TypeSafe lane: end-to-end through the pipeline against a scripted local
HTTP server that speaks the System One evaluation contract. No API key or
network needed in CI."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from detangle.config import Config, load_config
from detangle.lanes.typesafe import RELATION_CRITERIA, _conflict_mass, _verdict
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
    (both orderings), everything else distinct."""

    def __init__(self):
        self.calls = 0
        self.questions_seen = 0
        self.fail_first = False

    def answer(self, body: dict) -> dict:
        self.calls += 1
        self.questions_seen += len(body["questions"])
        units = body["state"]["units"]
        answers = {}
        for qid, q in body["questions"].items():
            assert q["type"] == "choice" and q["criteria"] == RELATION_CRITERIA
            _, a, b = qid.split("_", 2)
            texts = units[a]["text"] + " " + units[b]["text"]
            if "linter first" in texts and "Start with the test suite" in texts:
                probs = dict.fromkeys(RELATION_CRITERIA, 0.0)
                probs["contradictory"] = 0.92
                probs["conditional_conflict"] = 0.05
                probs["distinct"] = 0.03
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
            out = json.dumps(scripted.answer(body)).encode()
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
    assert scripted.calls == 1, "all pairs of a small config batch into one request"
    # every pair asked in both orderings inside that one request
    assert scripted.questions_seen % 2 == 0 and scripted.questions_seen >= 2
    assert any("typesafe lane: judged" in n for n in r1.corpus.notes)

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
    the NLI channel; confident verdicts never are."""
    scripted, endpoint = server
    monkeypatch.setenv("TS_TEST_KEY", "test-key")
    write_tree(tmp_path, TREE)
    captured = {}

    def fake_jury(cfg, ctx, findings):
        captured["band"] = list(getattr(ctx, "nli_not_cleared", None) or [])
        return findings

    monkeypatch.setattr("detangle.lanes.jury.run_jury_lane", fake_jury)
    # tau above the scripted 0.97 conflict mass -> the pair is "uncertain"
    cfg = _cfg(tmp_path, endpoint, lane_jury=True, typesafe_tau=0.99, typesafe_strong=0.995)
    r = scan(cfg)
    assert not [f for f in r.findings if "typesafe" in f.lanes]
    assert len(captured["band"]) == 1
    pair, mass = captured["band"][0]
    assert mass == pytest.approx(0.97)


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


def test_config_parsing_and_validation(tmp_path: Path):
    (tmp_path / ".detangle.toml").write_text(
        "[detangle.lanes]\ntypesafe = true\n"
        "[detangle.typesafe]\nmodel = 'jev-2'\npairs = 'candidates'\ntau = 0.6\n"
    )
    cfg = load_config(tmp_path)
    assert cfg.lane_typesafe and cfg.typesafe_model == "jev-2"
    assert cfg.typesafe_pairs == "candidates" and cfg.typesafe_tau == 0.6
    (tmp_path / ".detangle.toml").write_text("[detangle.typesafe]\ntau = 0.95\nstrong = 0.5\n")
    with pytest.raises(Exception, match="thresholds"):
        load_config(tmp_path)
    (tmp_path / ".detangle.toml").write_text("[detangle.typesafe]\npairs = 'some'\n")
    with pytest.raises(Exception, match="pairs"):
        load_config(tmp_path)
