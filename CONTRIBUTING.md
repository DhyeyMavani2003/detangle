# Contributing to detangle

## Set up

```bash
git clone https://github.com/DhyeyMavani2003/detangle
cd detangle
pip install -e '.[dev]'           # add '.[jury]' only to work on the Anthropic API backend
detangle scan examples/demo-agent # a realistic config with 14 planted conflicts
```

Before every commit:

```bash
ruff check src/ tests/ benchmarks/
ruff format src/ tests/ benchmarks/
pytest -q
python -m benchmarks.run_eval     # recall and false positives; must not regress
```

## How a scan works

`src/detangle/pipeline.py` runs one straight line:

1. **Ingest** (`ingest/`): one parser per ecosystem (`claude_code.py`, `agentsmd.py`,
   `cursor.py`, `copilot.py`) finds config files and records which tools read each one.
2. **Extract** (`extract.py`, `lexicons.py`): splits files into instruction units and reads
   each unit's modality (must, must not, may), action, object, numbers and scope.
3. **Co-activate** (`activation.py`): decides whether two units can ever be in context
   together, and what the ecosystem says about which one wins.
4. **Block** (`candidates.py`): forms candidate pairs cheaply, so detectors never compare
   everything with everything.
5. **Detect** (`detectors/`): the rules, run in claim-priority order so one root cause yields
   one finding.
6. **Optional lanes** (`lanes/`): TypeSafe, then the experimental screen and jury. The
   deterministic core never imports from here.
7. **Suppress, baseline, report** (`suppress.py`, `baseline.py`, `report.py`).

Where each rule lives:

| module | codes |
|---|---|
| `detectors/conflicts.py` (the conflict router; shared clash tests in `disagreement.py`) | DTC01–05, DTC08, DTP01–04, DTX02 |
| `detectors/redundancy.py` | DTR01–03 |
| `detectors/hygiene.py` | DTP06, DTR04, DTR05, DTX01 |
| `detectors/routing.py` | DTP05, DTS01–03 |

DTC06 and DTC07 are reserved: no detector emits them yet. Every code is described in
[docs/taxonomy.md](docs/taxonomy.md).

## Changing a detector

- Keep findings precision-first: when unsure whether a detector should fire, it should not.
- Every fix needs a seeded case in `tests/test_detectors.py`, plus a close-but-benign
  control that must **not** fire. The `scan_factory` fixture in `tests/conftest.py` builds
  a config tree in a temp directory and scans it.
- The deterministic lane stays dependency-light: no ML or network imports outside
  `src/detangle/lanes/`.
- Run the benchmark before and after. The holdout in `benchmarks/holdout.py` is small and has
  already been used to tune the TypeSafe lane, so treat a one-case change as noise and read
  [docs/benchmark.md](docs/benchmark.md) before claiming an improvement.

## Changing a lane

Read [docs/lanes.md](docs/lanes.md) for what each lane does and
[docs/experiments.md](docs/experiments.md) for what has already been measured, including the
ideas that were rejected and why. Lane tests fake the network: `tests/test_typesafe_lane.py`
runs a scripted local server, and `tests/test_backends.py` fakes each LLM transport.
