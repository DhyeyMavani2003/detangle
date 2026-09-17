"""LLM backends for the jury lane.

The jury protocol is backend-agnostic: a backend is anything that can take
(system prompt, user prompt) and return raw text, at temperature 0 wherever
the transport still accepts a sampling parameter (the 1.x Anthropic SDK has
none; determinism is protocol-engineered, never assumed). Three
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

import inspect
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


# Output budget per lane role. A jury verdict is one small JSON object; a
# screen sweep returns a nomination ARRAY whose length scales with the config,
# and a budget that truncates it silently drops nominations (the parser sees
# an unterminated array and yields nothing), so the screen role gets room.
DEFAULT_MAX_TOKENS = 500
ROLE_MAX_TOKENS = {"jury": DEFAULT_MAX_TOKENS, "screen": 4096}


class Backend:
    """Base: complete(system, user) -> raw model text."""

    name = "base"
    model = ""
    max_tokens = DEFAULT_MAX_TOKENS

    @property
    def ident(self) -> str:
        """Cache-key identity: switching backend or model invalidates verdicts."""
        return f"{self.name}:{self.model}"

    def complete(self, system: str, user: str) -> str:  # pragma: no cover
        raise NotImplementedError


def _accepts_temperature(client: object) -> bool:
    """True when ``client.messages.create`` still has a ``temperature`` parameter."""
    try:
        params = inspect.signature(client.messages.create).parameters  # type: ignore[attr-defined]
    except (AttributeError, TypeError, ValueError):
        return False
    return "temperature" in params


class AnthropicBackend(Backend):
    name = "anthropic"

    def __init__(self, model: str, max_tokens: int = DEFAULT_MAX_TOKENS):
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
        self.max_tokens = max_tokens
        # The 0.x SDK takes ``temperature``; the 1.x SDK (Claude 5 API
        # generation) dropped every sampling parameter, and passing one is a
        # TypeError that would fail every call. Send it only where accepted.
        self._sampling = {"temperature": 0} if _accepts_temperature(self.client) else {}

    def complete(self, system: str, user: str) -> str:
        try:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                **self._sampling,
            )
        except Exception as e:
            # an SDK error (unknown model id, auth, network) must degrade the
            # lane like any backend failure, not abort the scan with a traceback
            raise JuryError(f"anthropic backend: {type(e).__name__}: {str(e)[:200]}") from e
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
        max_tokens: int = DEFAULT_MAX_TOKENS,
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
        self.max_tokens = max_tokens

    @property
    def ident(self) -> str:
        return f"{self.name}:{self.base_url}:{self.model}"

    def complete(self, system: str, user: str) -> str:
        body = json.dumps(
            {
                "model": self.model,
                "temperature": 0,
                "max_tokens": self.max_tokens,
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


# model aliases that make sense per backend when the user did not choose one.
# The screen role defaults to the STRONGEST tier: whole-config reasoning is
# exactly where model quality buys recall.
_DEFAULT_MODELS = {
    "jury": {
        "anthropic": "claude-haiku-4-5-20251001",
        "claude-cli": "haiku",
        "openai": "gpt-5-mini",
    },
    "screen": {
        "anthropic": "claude-opus-5",
        "claude-cli": "opus",
        "openai": "gpt-5",
    },
}


def make_backend(cfg: Config, role: str = "jury") -> Backend:
    """Resolve the configured (or auto-detected) backend for a lane role.

    ``role`` is "jury" (pair adjudication; cfg.jury_model) or "screen"
    (whole-config sweep; cfg.screen_model, defaulting to the strongest tier).
    Both roles share the same transport choice (cfg.jury_backend).
    """
    choice = cfg.jury_backend
    if role == "screen":
        model = cfg.screen_model
    else:
        model = cfg.jury_model

    def pick_model(backend_name: str) -> str:
        # the stored jury default is anthropic-shaped; translate per backend,
        # and let each role fall back to its own tier
        if role == "screen":
            return model or _DEFAULT_MODELS["screen"][backend_name]
        if model and model != Config.jury_model:
            return model
        return _DEFAULT_MODELS["jury"][backend_name]

    budget = ROLE_MAX_TOKENS.get(role, DEFAULT_MAX_TOKENS)

    if choice == "anthropic":
        return AnthropicBackend(pick_model("anthropic"), max_tokens=budget)
    if choice == "claude-cli":
        return ClaudeCliBackend(pick_model("claude-cli"))
    if choice == "openai":
        return OpenAICompatBackend(
            pick_model("openai"), cfg.jury_base_url, cfg.jury_api_key_env, max_tokens=budget
        )
    if choice != "auto":
        raise JuryError(
            f"unknown jury backend '{choice}' (expected auto, anthropic, claude-cli, or openai)"
        )

    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return AnthropicBackend(pick_model("anthropic"), max_tokens=budget)
        except JuryError:
            pass
    if shutil.which("claude"):
        return ClaudeCliBackend(pick_model("claude-cli"))
    if cfg.jury_base_url:
        return OpenAICompatBackend(
            pick_model("openai"), cfg.jury_base_url, cfg.jury_api_key_env, max_tokens=budget
        )
    raise JuryError(
        "no jury backend available: set ANTHROPIC_API_KEY (anthropic), install the "
        "Claude Code CLI (claude-cli), or configure [detangle.jury] base_url (openai)"
    )
