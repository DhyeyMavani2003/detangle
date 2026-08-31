"""Configuration: .detangle.toml discovery, defaults, CLI overrides."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - py310 fallback
    import tomli as tomllib

from .taxonomy import RULES, Severity

CONFIG_FILENAMES = (".detangle.toml", "detangle.toml")

_SEVERITY_NAMES = {s.label: s for s in Severity}


@dataclass
class Config:
    """Resolved configuration for a run."""

    root: Path = field(default_factory=Path.cwd)
    ecosystems: tuple[str, ...] = ("claude-code", "agents-md", "cursor", "copilot")
    # Lanes: deterministic is always on; nli/jury are opt-in.
    lane_nli: bool = False
    lane_jury: bool = False
    lane_screen: bool = False  # whole-config LLM sweep; implies the jury
    screen_model: str = ""  # backend-shaped; empty = backend's strong default
    include_soft: bool = True  # report advisory-tier findings
    fail_on: Severity = Severity.ERROR  # exit non-zero at or above this severity
    conflict_budget: int | None = None  # allowed open findings before failure (ratchet)
    disabled_rules: frozenset[str] = frozenset()
    severity_overrides: dict[str, Severity] = field(default_factory=dict)
    max_pairs: int = 250_000  # hard cap on candidate pairs (safety valve)
    similarity_threshold: float = 0.18  # blocking floor for lexical similarity pairs
    user_dir: Path | None = None  # simulated ~ for user-global layers (tests/CI)
    jury_model: str = "claude-haiku-4-5-20251001"
    jury_max_pairs: int = 200
    jury_backend: str = "auto"  # auto | anthropic | claude-cli | openai
    jury_base_url: str = ""  # for the openai-compatible backend
    jury_api_key_env: str = "OPENAI_API_KEY"
    nli_model: str = "cross-encoder/nli-deberta-v3-small"
    cache_dir: Path | None = None
    ignore_globs: tuple[str, ...] = ()  # config files to skip entirely
    respect_gitignore: bool = True
    # Deep pass: thoroughness-first profile — every available lane on,
    # per-class screen sweeps, jury cap lifted. Hours are acceptable.
    deep: bool = False
    # Triage baseline: a checked-in JSON artifact carrying human verdicts
    # across runs (new/open/accepted/resolved), used to pre-fill triage and
    # focus reports on what's new. None = no baseline.
    baseline_path: Path | None = None
    update_baseline: bool = False  # write the merged baseline back after the scan
    only_new: bool = False  # report only new/regression findings
    fail_on_new: bool = False  # exit non-zero only for new/regression findings

    def severity_for(self, code: str) -> Severity:
        if code in self.severity_overrides:
            return self.severity_overrides[code]
        r = RULES.get(code)
        return r.default_severity if r else Severity.WARNING

    def rule_enabled(self, code: str) -> bool:
        return code not in self.disabled_rules


class ConfigError(ValueError):
    pass


def find_config_file(root: Path) -> Path | None:
    for name in CONFIG_FILENAMES:
        p = root / name
        if p.is_file():
            return p
    return None


def load_config(root: Path, path: Path | None = None) -> Config:
    """Load config from ``path`` or by discovery under ``root``; defaults if absent."""
    root = root.resolve()
    cfg_path = path or find_config_file(root)
    cfg = Config(root=root)
    if cfg_path is None:
        return cfg
    with open(cfg_path, "rb") as f:
        try:
            data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"{cfg_path}: invalid TOML: {e}") from e
    return _apply(cfg, data, cfg_path)


def _apply(cfg: Config, data: dict[str, Any], src: Path) -> Config:
    tbl = data.get("detangle", data)  # allow top-level or [detangle] table

    def bad(msg: str) -> ConfigError:
        return ConfigError(f"{src}: {msg}")

    def as_int(name: str, val: Any) -> int:
        try:
            return int(val)
        except (TypeError, ValueError):
            raise bad(f"'{name}' must be an integer, got {val!r}") from None

    def as_float(name: str, val: Any) -> float:
        try:
            return float(val)
        except (TypeError, ValueError):
            raise bad(f"'{name}' must be a number, got {val!r}") from None

    if "ecosystems" in tbl:
        eco = tbl["ecosystems"]
        if not isinstance(eco, list) or not all(isinstance(e, str) for e in eco):
            raise bad("'ecosystems' must be a list of strings")
        cfg.ecosystems = tuple(eco)

    lanes = tbl.get("lanes", {})
    if not isinstance(lanes, dict):
        raise bad("'lanes' must be a table")
    cfg.lane_nli = bool(lanes.get("nli", cfg.lane_nli))
    cfg.lane_jury = bool(lanes.get("jury", cfg.lane_jury))
    cfg.lane_screen = bool(lanes.get("screen", cfg.lane_screen))

    if "deep" in tbl:
        cfg.deep = bool(tbl["deep"])

    baseline = tbl.get("baseline", {})
    if not isinstance(baseline, dict):
        raise bad("'baseline' must be a table")
    if "path" in baseline:
        cfg.baseline_path = Path(str(baseline["path"]))
    if "update" in baseline:
        cfg.update_baseline = bool(baseline["update"])

    if "fail_on" in tbl:
        name = str(tbl["fail_on"]).lower()
        if name not in _SEVERITY_NAMES:
            raise bad(f"'fail_on' must be one of {sorted(_SEVERITY_NAMES)}")
        cfg.fail_on = _SEVERITY_NAMES[name]

    if "conflict_budget" in tbl:
        cfg.conflict_budget = as_int("conflict_budget", tbl["conflict_budget"])
    if "include_soft" in tbl:
        cfg.include_soft = bool(tbl["include_soft"])
    if "max_pairs" in tbl:
        cfg.max_pairs = as_int("max_pairs", tbl["max_pairs"])
    if "similarity_threshold" in tbl:
        cfg.similarity_threshold = as_float("similarity_threshold", tbl["similarity_threshold"])
    if "ignore" in tbl:
        ig = tbl["ignore"]
        if not isinstance(ig, list):
            raise bad("'ignore' must be a list of globs")
        cfg.ignore_globs = tuple(str(g) for g in ig)
    if "respect_gitignore" in tbl:
        cfg.respect_gitignore = bool(tbl["respect_gitignore"])

    rules = tbl.get("rules", {})
    if not isinstance(rules, dict):
        raise bad("'rules' must be a table")
    disabled: set[str] = set()
    for code, val in rules.items():
        code = code.upper()
        if code not in RULES:
            raise bad(f"unknown rule '{code}' in [rules]")
        if val is False or val == "off":
            disabled.add(code)
        elif isinstance(val, str):
            name = val.lower()
            if name not in _SEVERITY_NAMES:
                raise bad(f"rule '{code}': severity must be one of {sorted(_SEVERITY_NAMES)}")
            cfg.severity_overrides[code] = _SEVERITY_NAMES[name]
        elif val is not True:
            raise bad(f"rule '{code}': expected false, true, or a severity string")
    cfg.disabled_rules = frozenset(disabled)

    jury = tbl.get("jury", {})
    if isinstance(jury, dict):
        cfg.jury_model = str(jury.get("model", cfg.jury_model))
        cfg.jury_max_pairs = as_int("jury.max_pairs", jury.get("max_pairs", cfg.jury_max_pairs))
        backend = str(jury.get("backend", cfg.jury_backend))
        if backend not in ("auto", "anthropic", "claude-cli", "openai"):
            raise bad(
                f"jury.backend '{backend}' must be one of: auto, anthropic, claude-cli, openai"
            )
        cfg.jury_backend = backend
        cfg.jury_base_url = str(jury.get("base_url", cfg.jury_base_url))
        cfg.jury_api_key_env = str(jury.get("api_key_env", cfg.jury_api_key_env))

    screen = tbl.get("screen", {})
    if isinstance(screen, dict):
        cfg.screen_model = str(screen.get("model", cfg.screen_model))

    nli = tbl.get("nli", {})
    if isinstance(nli, dict):
        cfg.nli_model = str(nli.get("model", cfg.nli_model))

    return cfg
