# detangle — development guide

detangle is a static conflict linter for AI agent configurations. Pure Python 3.10+,
`src/` layout, deterministic core with optional NLI/jury lanes.

## Working here

- Run the test suite with `pytest -q` before committing.
- Lint with `ruff check src/ tests/ benchmarks/` and format with `ruff format`.
- The deterministic lane must stay dependency-light: no ML or network imports outside
  `src/detangle/lanes/`.
- Detector changes need a seeded case in `tests/test_detectors.py` covering the fix,
  including a close-but-benign control that must NOT fire.
- Keep findings precision-first: when unsure whether a detector should fire, it should not.

## Layout

- `src/detangle/ingest/` — per-ecosystem parsers (Claude Code, AGENTS.md, Cursor, Copilot)
- `src/detangle/detectors/` — the rule implementations; `disagreement.py` holds the shared
  clash tests
- `src/detangle/activation.py` — co-activation + precedence model
- `benchmarks/` — seeded-conflict evaluation harness (`python -m benchmarks.run_eval`)
- `docs/` — taxonomy, ecosystem semantics, lanes, configuration reference
