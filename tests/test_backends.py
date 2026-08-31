"""Tests for the jury LLM backends (transport mocked — no network, no CLI)."""

from __future__ import annotations

import io
import json
import urllib.request
from pathlib import Path

import pytest

from detangle.config import Config, ConfigError, load_config
from detangle.lanes.backends import (
    Backend,
    ClaudeCliBackend,
    JuryError,
    OpenAICompatBackend,
    make_backend,
)

# ---------------------------------------------------------------------------
# claude-cli backend
# ---------------------------------------------------------------------------


class _FakeProc:
    def __init__(self, stdout: str, returncode: int = 0, stderr: str = ""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def _cli_backend(monkeypatch) -> ClaudeCliBackend:
    monkeypatch.setattr("shutil.which", lambda _cli: "/usr/bin/claude")
    return ClaudeCliBackend(model="haiku")


class TestClaudeCliBackend:
    def test_missing_executable_raises(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda _cli: None)
        with pytest.raises(JuryError, match="no 'claude' executable"):
            ClaudeCliBackend()

    def test_happy_path_parses_result(self, monkeypatch):
        b = _cli_backend(monkeypatch)
        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["cwd"] = kwargs.get("cwd")
            return _FakeProc(json.dumps({"is_error": False, "result": '{"verdict": "DISTINCT"}'}))

        monkeypatch.setattr("subprocess.run", fake_run)
        out = b.complete("SYSTEM", "USER")
        assert out == '{"verdict": "DISTINCT"}'
        # print mode, appended system prompt, json output, chosen model
        assert "-p" in captured["cmd"] and "USER" in captured["cmd"]
        sys_idx = captured["cmd"].index("--append-system-prompt")
        assert "SYSTEM" in captured["cmd"][sys_idx + 1]
        assert captured["cmd"][captured["cmd"].index("--model") + 1] == "haiku"
        assert captured["cmd"][captured["cmd"].index("--output-format") + 1] == "json"
        # runs in an empty scratch dir, not the scanned repo
        assert captured["cwd"] and "detangle-jury-" in captured["cwd"]

    def test_nonzero_exit_raises(self, monkeypatch):
        b = _cli_backend(monkeypatch)
        monkeypatch.setattr(
            "subprocess.run", lambda *a, **k: _FakeProc("", returncode=1, stderr="boom")
        )
        with pytest.raises(JuryError, match="exited 1"):
            b.complete("s", "u")

    def test_error_payload_raises(self, monkeypatch):
        b = _cli_backend(monkeypatch)
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **k: _FakeProc(json.dumps({"is_error": True, "result": "nope"})),
        )
        with pytest.raises(JuryError, match="reported an error"):
            b.complete("s", "u")

    def test_non_json_stdout_raises(self, monkeypatch):
        b = _cli_backend(monkeypatch)
        monkeypatch.setattr("subprocess.run", lambda *a, **k: _FakeProc("garbage"))
        with pytest.raises(JuryError, match="non-JSON"):
            b.complete("s", "u")

    def test_ident_carries_backend_and_model(self, monkeypatch):
        assert _cli_backend(monkeypatch).ident == "claude-cli:haiku"


# ---------------------------------------------------------------------------
# openai-compatible backend
# ---------------------------------------------------------------------------


class TestOpenAICompatBackend:
    def test_requires_base_url(self):
        with pytest.raises(JuryError, match="base_url"):
            OpenAICompatBackend("gpt-5-mini", base_url="")

    def test_happy_path(self, monkeypatch):
        b = OpenAICompatBackend("m1", base_url="http://localhost:11434/v1")
        captured: dict = {}

        def fake_urlopen(req, timeout=0):
            captured["url"] = req.full_url
            captured["body"] = json.loads(req.data.decode())
            captured["auth"] = req.headers.get("Authorization")
            return io.BytesIO(
                json.dumps(
                    {"choices": [{"message": {"content": '{"verdict": "REDUNDANT"}'}}]}
                ).encode()
            )

        monkeypatch.setattr(urllib.request, "urlopen", _ContextWrap(fake_urlopen))
        out = b.complete("SYS", "USR")
        assert out == '{"verdict": "REDUNDANT"}'
        assert captured["url"] == "http://localhost:11434/v1/chat/completions"
        assert captured["body"]["model"] == "m1"
        assert captured["body"]["temperature"] == 0
        assert captured["body"]["messages"][0] == {"role": "system", "content": "SYS"}
        assert captured["auth"] is None  # no key set -> no auth header (Ollama-style)

    def test_bearer_header_from_env(self, monkeypatch):
        monkeypatch.setenv("MY_KEY", "sk-test")
        b = OpenAICompatBackend("m", base_url="https://api.example.com/v1", api_key_env="MY_KEY")
        seen: dict = {}

        def fake_urlopen(req, timeout=0):
            seen["auth"] = req.headers.get("Authorization")
            return io.BytesIO(json.dumps({"choices": [{"message": {"content": "x"}}]}).encode())

        monkeypatch.setattr(urllib.request, "urlopen", _ContextWrap(fake_urlopen))
        b.complete("s", "u")
        assert seen["auth"] == "Bearer sk-test"

    def test_bad_shape_raises(self, monkeypatch):
        b = OpenAICompatBackend("m", base_url="http://x/v1")
        monkeypatch.setattr(
            urllib.request,
            "urlopen",
            _ContextWrap(lambda req, timeout=0: io.BytesIO(b'{"weird": true}')),
        )
        with pytest.raises(JuryError, match="unexpected response shape"):
            b.complete("s", "u")

    def test_ident_includes_base_url(self):
        b = OpenAICompatBackend("m", base_url="http://x/v1")
        assert b.ident == "openai:http://x/v1:m"


class _ContextWrap:
    """urlopen returns a context manager; wrap a plain callable to match."""

    def __init__(self, fn):
        self.fn = fn

    def __call__(self, req, timeout=0):
        body = self.fn(req, timeout=timeout)

        class _CM:
            def __enter__(self_inner):
                return body

            def __exit__(self_inner, *exc):
                return False

        return _CM()


# ---------------------------------------------------------------------------
# backend resolution
# ---------------------------------------------------------------------------


class TestMakeBackend:
    def test_explicit_claude_cli(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda _c: "/usr/bin/claude")
        cfg = Config()
        cfg.jury_backend = "claude-cli"
        b = make_backend(cfg)
        assert isinstance(b, ClaudeCliBackend)
        # anthropic-shaped default model translates to the CLI alias
        assert b.model == "haiku"

    def test_explicit_model_passes_through(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda _c: "/usr/bin/claude")
        cfg = Config()
        cfg.jury_backend = "claude-cli"
        cfg.jury_model = "sonnet"
        assert make_backend(cfg).model == "sonnet"

    def test_explicit_openai(self):
        cfg = Config()
        cfg.jury_backend = "openai"
        cfg.jury_base_url = "http://localhost:11434/v1"
        b = make_backend(cfg)
        assert isinstance(b, OpenAICompatBackend)

    def test_auto_prefers_cli_when_no_key(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr("shutil.which", lambda _c: "/usr/bin/claude")
        cfg = Config()
        assert isinstance(make_backend(cfg), ClaudeCliBackend)

    def test_auto_falls_back_to_base_url(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr("shutil.which", lambda _c: None)
        cfg = Config()
        cfg.jury_base_url = "http://x/v1"
        assert isinstance(make_backend(cfg), OpenAICompatBackend)

    def test_auto_with_nothing_raises(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setattr("shutil.which", lambda _c: None)
        with pytest.raises(JuryError, match="no jury backend available"):
            make_backend(Config())

    def test_unknown_backend_raises(self):
        cfg = Config()
        cfg.jury_backend = "llamacpp"
        with pytest.raises(JuryError, match="unknown jury backend"):
            make_backend(cfg)


# ---------------------------------------------------------------------------
# config plumbing
# ---------------------------------------------------------------------------


class TestJuryBackendConfig:
    def test_toml_keys(self, tmp_path: Path):
        (tmp_path / ".detangle.toml").write_text(
            "[detangle.jury]\n"
            'backend = "openai"\n'
            'base_url = "http://localhost:8000/v1"\n'
            'api_key_env = "VLLM_KEY"\n'
            'model = "qwen3"\n'
        )
        cfg = load_config(tmp_path)
        assert cfg.jury_backend == "openai"
        assert cfg.jury_base_url == "http://localhost:8000/v1"
        assert cfg.jury_api_key_env == "VLLM_KEY"
        assert cfg.jury_model == "qwen3"

    def test_invalid_backend_rejected(self, tmp_path: Path):
        (tmp_path / ".detangle.toml").write_text('[detangle.jury]\nbackend = "bogus"\n')
        with pytest.raises(ConfigError, match="jury.backend"):
            load_config(tmp_path)


# ---------------------------------------------------------------------------
# juror integration with a scripted fake backend (swap rules, cache)
# ---------------------------------------------------------------------------


class _ScriptedBackend(Backend):
    name = "fake"
    model = "scripted"

    def __init__(self, responses: list[str]):
        self.responses = list(responses)
        self.calls = 0

    def complete(self, system: str, user: str) -> str:
        self.calls += 1
        return self.responses.pop(0)


def _verdict_json(verdict: str, ea: str, eb: str, confidence: float = 0.9) -> str:
    return json.dumps(
        {
            "overlap_condition": "always",
            "evidence_a": ea,
            "evidence_b": eb,
            "reasoning_summary": "r",
            "verdict": verdict,
            "conflict_type": "negation",
            "resolution_hint": "h",
            "confidence": confidence,
        }
    )


def _make_pair():
    from detangle.activation import build_pair
    from detangle.ir import (
        Activation,
        ActivationMode,
        ConfigFile,
        Ecosystem,
        InstructionUnit,
        Layer,
        SourceSpan,
    )

    def unit(text: str, path: str, line: int) -> InstructionUnit:
        cf = ConfigFile(
            path=path,
            ecosystem=Ecosystem.CLAUDE_CODE,
            layer=Layer.PROJECT,
            tier=20,
            activation=Activation(mode=ActivationMode.ALWAYS),
            text=text,
            mechanism="memory",
            tool="claude-code",
        )
        cf.meta["readers"] = ("claude-code",)
        return InstructionUnit(
            text=text,
            normalized=text,
            span=SourceSpan(path, line, line),
            file=cf,
            activation=cf.activation,
        )

    a = unit("Always use tabs for indentation.", "CLAUDE.md", 3)
    b = unit("Never use tabs for indentation.", "AGENTS.md", 5)
    return build_pair(a, b)


class _DictCache:
    def __init__(self):
        self.data = {}

    def key(self, model, prompt_hash, pair_key):
        return f"{model}|{prompt_hash}|{pair_key}"

    def get(self, key):
        return self.data.get(key)

    def put(self, key, value):
        self.data[key] = value

    def save(self):
        pass


class TestJurorSwapRules:
    def _adjudicate(self, responses: list[str]):
        from detangle.lanes.jury import Juror, adjudicate

        backend = _ScriptedBackend(responses)
        cache = _DictCache()
        result = adjudicate(Juror(backend), _make_pair(), cache)
        return result, backend, cache

    def test_agreeing_verdicts_pass(self):
        r = _verdict_json("CONTRADICTORY", "Always use tabs", "Never use tabs")
        result, backend, _ = self._adjudicate([r, r])
        assert result["verdict"] == "CONTRADICTORY" and not result["abstained"]
        assert backend.calls == 2  # both orderings judged

    def test_cross_group_instability_abstains(self):
        r1 = _verdict_json("CONTRADICTORY", "Always use tabs", "Never use tabs")
        r2 = _verdict_json("DISTINCT", "Always use tabs", "Never use tabs")
        result, _, _ = self._adjudicate([r1, r2])
        assert result["abstained"] and "order instability" in result["reason"]

    def test_within_conflict_group_softens_to_conditional(self):
        r1 = _verdict_json("CONTRADICTORY", "Always use tabs", "Never use tabs", 0.95)
        # swapped ordering: evidence_a now quotes B's text
        r2 = _verdict_json("CONDITIONAL_CONFLICT", "Never use tabs", "Always use tabs", 0.7)
        result, _, _ = self._adjudicate([r1, r2])
        assert result["verdict"] == "CONDITIONAL_CONFLICT"
        assert not result["abstained"]
        assert result["swap_softened"] is True
        assert result["confidence"] == 0.7  # min of the two

    def test_fabricated_evidence_abstains(self):
        r = _verdict_json("CONTRADICTORY", "quote not in source", "also missing")
        result, _, _ = self._adjudicate([r, r])
        assert result["abstained"] and "evidence" in result["reason"]

    def test_cache_prevents_second_round(self):
        from detangle.lanes.jury import Juror, adjudicate

        r = _verdict_json("CONTRADICTORY", "Always use tabs", "Never use tabs")
        backend = _ScriptedBackend([r, r])
        cache = _DictCache()
        pair = _make_pair()
        first = adjudicate(Juror(backend), pair, cache)
        second = adjudicate(Juror(backend), pair, cache)
        assert backend.calls == 2  # second adjudication fully served from cache
        assert first == second

    def test_backend_error_is_transient_and_uncached(self):
        from detangle.lanes.jury import Juror, adjudicate

        class _Boom(Backend):
            name = "fake"
            model = "boom"

            def complete(self, system: str, user: str) -> str:
                raise JuryError("transport down")

        cache = _DictCache()
        result = adjudicate(Juror(_Boom()), _make_pair(), cache)
        assert result["abstained"] and result.get("transient") is True
        assert cache.data == {}  # transient failures are never cached
