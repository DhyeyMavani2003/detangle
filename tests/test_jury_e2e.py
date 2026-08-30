"""End-to-end jury pipeline test over a REAL local HTTP server.

Runs the full scan pipeline (deterministic detectors -> jury lane) against a
live OpenAI-compatible endpoint served from a background thread. This proves
the whole chain — config, backend resolution, transport, prompt assembly,
verdict parsing, order-swap, evidence validation, caching, finding emission —
with a scripted model, so it runs in CI with no keys and no network.
"""

from __future__ import annotations

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from detangle.config import Config
from detangle.pipeline import scan

from .conftest import write_tree


class _ScriptedJurorHandler(BaseHTTPRequestHandler):
    """Answers /chat/completions with a CONTRADICTORY verdict whose evidence
    quotes are lifted from the actual instruction texts in the request — so
    the lane's evidence validation passes exactly as it would with a real
    model that quotes its sources."""

    calls: list[dict] = []

    def do_POST(self):  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        type(self).calls.append(body)
        user_msg = next(m["content"] for m in body["messages"] if m["role"] == "user")
        texts = re.findall(r'text: "((?:[^"\\]|\\.)*)"', user_msg)
        ev_a = json.loads(f'"{texts[0]}"') if texts else ""
        ev_b = json.loads(f'"{texts[1]}"') if len(texts) > 1 else ""
        verdict = {
            "overlap_condition": "always",
            "evidence_a": ev_a,
            "evidence_b": ev_b,
            "reasoning_summary": "scripted",
            "verdict": "CONTRADICTORY",
            "conflict_type": "negation",
            "resolution_hint": "pick one",
            "confidence": 0.91,
        }
        payload = json.dumps(
            {"choices": [{"message": {"content": json.dumps(verdict)}}]}
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # silence request logging
        pass


def test_jury_pipeline_end_to_end_over_http(tmp_path: Path):
    _ScriptedJurorHandler.calls = []
    server = HTTPServer(("127.0.0.1", 0), _ScriptedJurorHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # a pair that BLOCKS together (shared action 'cite') but that the
        # deterministic lane does NOT claim ('cite' is outside the verb
        # whitelist and the objects differ) — so it reaches the jury unclaimed
        write_tree(
            tmp_path,
            {
                "CLAUDE.md": (
                    "# Rules\n\n"
                    "- Always cite the documentation when explaining code.\n"
                    "- Never cite anything in replies.\n"
                ),
            },
        )
        cfg = Config(root=tmp_path)
        cfg.lane_jury = True
        cfg.jury_backend = "openai"
        cfg.jury_base_url = f"http://127.0.0.1:{port}/v1"
        cfg.jury_model = "scripted-1"
        cfg.jury_max_pairs = 4
        result = scan(cfg)

        jury_findings = [f for f in result.findings if "jury" in f.lanes]
        assert jury_findings, f"no jury findings; notes={result.corpus.notes}"
        f = jury_findings[0]
        assert f.code == "DTC01"
        assert f.confidence == 0.91
        assert "scripted" in f.message
        # both orderings were judged for each adjudicated pair
        assert len(_ScriptedJurorHandler.calls) % 2 == 0
        assert len(_ScriptedJurorHandler.calls) >= 2
        # requests carried the jury protocol shape
        first = _ScriptedJurorHandler.calls[0]
        assert first["model"] == "scripted-1"
        assert first["temperature"] == 0
        assert first["messages"][0]["role"] == "system"
        assert "classify" in first["messages"][0]["content"].lower()
        # verdict cache: a second scan makes zero additional HTTP calls
        before = len(_ScriptedJurorHandler.calls)
        result2 = scan(cfg)
        assert len(_ScriptedJurorHandler.calls) == before
        assert [f.fingerprint for f in result2.findings] == [
            f.fingerprint for f in result.findings
        ]
    finally:
        server.shutdown()
        server.server_close()
