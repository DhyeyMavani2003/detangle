"""LLM backends for the jury lane.

The jury protocol is backend-agnostic: a backend is anything that can take
(system prompt, user prompt) and return raw text at temperature 0. Three
implementations ship, so the jury runs on whatever access you have:

- ``anthropic``   — the Anthropic API (needs ``detangle[jury]`` and
                    ``ANTHROPIC_API_KEY``).
- ``claude-cli``  — the Claude Code CLI in print mode (``claude -p``): if you
                    use Claude Code, this rides your existing subscription
                    with ZERO extra configuration or dependencies.
- ``openai``      — any OpenAI-compatible chat-completions endpoint (OpenAI,
                    DeepSeek, Gemini's compat layer, Ollama, vLLM, ...) via
                    stdlib urllib; point ``base_url`` wherever you like.

``backend = "auto"`` (the default) picks the first available:
ANTHROPIC_API_KEY -> anthropic, else a ``claude`` executable on PATH ->
claude-cli, else a configured ``base_url`` -> openai, else the lane skips
with a note.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request

from ..config import Config


class JuryError(RuntimeError):
    pass


class Backend:
    """Base: complete(system, user) -> raw model text."""

    name = "base"
    model = ""

    @property
    def ident(self) -> str:
        """Cache-key identity: switching backend or model invalidates verdicts."""
        return f"{self.name}:{self.model}"

    def complete(self, system: str, user: str) -> str:  # pragma: no cover
        raise NotImplementedError


class AnthropicBackend(Backend):
    name = "anthropic"

    def __init__(self, model: str):
        try:
            import anthropic  # type: ignore[import-not-found]
        except ImportError as e:
            raise JuryError(
                "anthropic backend requires the anthropic package — install `detangle[jury]`"
            ) from e
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise JuryError("anthropic backend requires ANTHROPIC_API_KEY in the environment")
        self.client = anthropic.Anthropic()
        self.model = model

    def complete(self, system: str, user: str) -> str:
        resp = self.client.messages.create(
            model=self.model,
            max_tokens=500,
            temperature=0,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(getattr(b, "text", "") for b in resp.content)


class ClaudeCliBackend(Backend):
    """``claude -p`` print mode: the user's Claude Code subscription is the juror.

    Runs in an empty scratch directory so the CLI does not ingest the scanned
    repo's own CLAUDE.md into the juror's context, and with the jury system
    prompt appended so the coding-agent persona yields to the classification
    task.
    """

    name = "claude-cli"

    def __init__(self, model: str = "haiku", cli: str = "claude", timeout: int = 180):
        path = shutil.which(cli)
        if path is None:
            raise JuryError(f"claude-cli backend: no '{cli}' executable on PATH")
        self.cli = path
        self.model = model
        self.timeout = timeout
        self._workdir = tempfile.mkdtemp(prefix="detangle-jury-")

    def complete(self, system: str, user: str) -> str:
        cmd = [
            self.cli,
            "-p",
            user,
            "--append-system-prompt",
            system + "\n\nThis classification task is your ONLY task. Do not use any tools. "
            "Respond with the JSON object only.",
            "--model",
            self.model,
            "--output-format",
            "json",
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=self._workdir,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            raise JuryError(f"claude-cli call failed: {e!r}") from e
        if proc.returncode != 0:
            raise JuryError(
                f"claude-cli exited {proc.returncode}: {(proc.stderr or proc.stdout)[:300]}"
            )
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise JuryError(f"claude-cli returned non-JSON output: {proc.stdout[:200]!r}") from e
        if not isinstance(payload, dict) or payload.get("is_error"):
            raise JuryError(f"claude-cli reported an error: {str(payload)[:300]}")
        return str(payload.get("result", ""))


class OpenAICompatBackend(Backend):
    """Any OpenAI-compatible /chat/completions endpoint, via stdlib urllib."""

    name = "openai"

    def __init__(
        self,
        model: str,
        base_url: str,
        api_key_env: str = "OPENAI_API_KEY",
        timeout: int = 120,
    ):
        if not base_url:
            raise JuryError(
                "openai backend requires [detangle.jury] base_url "
                '(e.g. "https://api.openai.com/v1" or "http://localhost:11434/v1")'
            )
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = os.environ.get(api_key_env, "")
        self.timeout = timeout

    @property
    def ident(self) -> str:
        return f"{self.name}:{self.base_url}:{self.model}"

    def complete(self, system: str, user: str) -> str:
        body = json.dumps(
            {
                "model": self.model,
                "temperature": 0,
                "max_tokens": 500,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            }
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=body, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            raise JuryError(f"openai-compatible call failed: {e!r}") from e
        try:
            return str(payload["choices"][0]["message"]["content"] or "")
        except (KeyError, IndexError, TypeError) as e:
            raise JuryError(f"unexpected response shape: {str(payload)[:300]}") from e


# model aliases that make sense per backend when the user did not choose one
_DEFAULT_MODELS = {
    "anthropic": "claude-haiku-4-5-20251001",
    "claude-cli": "haiku",
    "openai": "gpt-5-mini",
}


def make_backend(cfg: Config) -> Backend:
    """Resolve the configured (or auto-detected) jury backend."""
    choice = cfg.jury_backend
    model = cfg.jury_model

    def pick_model(backend_name: str) -> str:
        # the stored default is anthropic-shaped; translate for other backends
        if model and model != Config.jury_model:
            return model
        return _DEFAULT_MODELS[backend_name]

    if choice == "anthropic":
        return AnthropicBackend(pick_model("anthropic"))
    if choice == "claude-cli":
        return ClaudeCliBackend(pick_model("claude-cli"))
    if choice == "openai":
        return OpenAICompatBackend(pick_model("openai"), cfg.jury_base_url, cfg.jury_api_key_env)
    if choice != "auto":
        raise JuryError(
            f"unknown jury backend '{choice}' (expected auto, anthropic, claude-cli, or openai)"
        )

    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return AnthropicBackend(pick_model("anthropic"))
        except JuryError:
            pass
    if shutil.which("claude"):
        return ClaudeCliBackend(pick_model("claude-cli"))
    if cfg.jury_base_url:
        return OpenAICompatBackend(pick_model("openai"), cfg.jury_base_url, cfg.jury_api_key_env)
    raise JuryError(
        "no jury backend available: set ANTHROPIC_API_KEY (anthropic), install the "
        "Claude Code CLI (claude-cli), or configure [detangle.jury] base_url (openai)"
    )
