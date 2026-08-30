"""Cache construction shared by lanes."""

from __future__ import annotations

from pathlib import Path

from ..cache import VerdictCache
from ..config import Config


def make_cache(cfg: Config) -> VerdictCache:
    cache_dir = cfg.cache_dir or (cfg.root / ".detangle-cache")
    return VerdictCache(Path(cache_dir))
