"""Content-hash verdict cache (JSON file; survives runs, keys carry versions)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import __version__


class VerdictCache:
    """Cache keyed (linter version, model, prompt hash, pair key)."""

    def __init__(self, cache_dir: Path):
        self.path = cache_dir / "verdicts.json"
        self._data: dict[str, Any] = {}
        if self.path.is_file():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._data = {}
        self._dirty = False

    @staticmethod
    def key(model: str, prompt_hash: str, pair_key: str) -> str:
        return f"{__version__}|{model}|{prompt_hash}|{pair_key}"

    def get(self, key: str) -> Any | None:
        return self._data.get(key)

    def put(self, key: str, value: Any) -> None:
        self._data[key] = value
        self._dirty = True

    def save(self) -> None:
        if not self._dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=0), encoding="utf-8")
        self._dirty = False
