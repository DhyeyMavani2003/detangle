# Configuration reference

detangle is configured by (in order of application):

1. built-in defaults,
2. a TOML config file at the scan root,
3. command-line flags (which override the file where they overlap).

## Config file discovery

detangle looks for **`.detangle.toml`** first, then **`detangle.toml`**, in the directory
being scanned. `--config PATH` points at an explicit file instead. If no file exists,
defaults apply. Keys may live at the top level or under a `[detangle]` table — both of these
are equivalent:

```toml
fail_on = "warning"
```

```toml
[detangle]
fail_on = "warning"
```

Invalid TOML or invalid values are a hard configuration error: the message names the file and
the offending key, and the process exits with code **2**.

## Full example

```toml
[detangle]
ecosystems = ["claude-code", "agents-md", "cursor", "copilot"]
fail_on = "error"
conflict_budget = 25
include_soft = true
max_pairs = 250000
similarity_threshold = 0.18
ignore = ["vendor/**", "docs/generated/**"]
respect_gitignore = true

[detangle.lanes]
nli = false
jury = false

[detangle.rules]
DTR04 = false        # disable a rule entirely
DTC08 = "info"       # override a rule's severity
DTC01 = true         # explicit "keep enabled" (no-op)

[detangle.jury]
model = "claude-haiku-4-5-20251001"
max_pairs = 200
```

## Keys

### `ecosystems` — array of strings, default `["claude-code", "agents-md", "cursor", "copilot"]`

Which ecosystem parsers run during discovery. Valid names are the four defaults (see
[ecosystems.md](ecosystems.md)). An unknown name is **not** a config error — it is skipped
with a warning note at scan time. Must be a list of strings, else a config error.

### `fail_on` — string, default `"error"`

The severity at or above which findings make the exit code non-zero. One of `"info"`,
`"advisory"`, `"warning"`, `"error"` (the full ladder, in ascending order). Any other value
is a config error. Overridable per run with `--fail-on`.

### `conflict_budget` — integer, default unset

A ratchet for gradual adoption: when set, the run fails (exit 1) if the number of reported
findings — of *any* severity — exceeds the budget, even if none reach `fail_on`. Leave unset
to disable.

### `include_soft` — boolean, default `true`

When `false`, advisory- and info-tier findings are dropped from the results entirely (only
`warning` and `error` remain). Equivalent to the `--no-soft` flag.

### `max_pairs` — integer, default `250000`

Hard cap on candidate pairs produced by blocking — a safety valve for pathological repos.
Pair generation stops once the cap is reached.

### `similarity_threshold` — float, default `0.18`

The blocking floor for the lexical-similarity candidate pass: two units become a candidate
pair via similarity alone only when their blended token/character similarity is at or above
this value. (Pairs can still qualify through the other blocking passes — shared topic, shared
action/object, shared quantity subject, shared defined term.) Lowering it increases recall
and scan time.

### `ignore` — array of glob strings, default `[]`

Repo-relative path globs (fnmatch syntax) excluded from the repository walk. Matching files
are invisible to everything driven by the walk: AGENTS.md discovery, subdirectory CLAUDE.md
discovery, nested `.cursor/rules` discovery, the Zed first-match computation, dead-glob
checks, and stale-reference existence checks. Must be a list, else a config error.

Caveat (current behavior): fixed-location files probed directly — the root `CLAUDE.md` /
`.claude/CLAUDE.md` / `CLAUDE.local.md`, `.claude/rules|skills|agents|commands`,
`.cursorrules`, a root `.cursor/rules` directory, and the `.github` Copilot files — are
discovered by direct path checks and are **not** excluded by `ignore`.

### `respect_gitignore` — boolean, default `true`

When `true`, the discovery walk reads the repo-root `.gitignore` and skips files and
directories its patterns match (negation lines starting with `!` are ignored —
precision-first, so a negated re-include is simply not excluded in the first place).
The walker additionally always skips a built-in list of vendored/derived directories
(`.git`, `node_modules`, `.venv`, `dist`, `build`, caches, …) and applies `ignore`
globs. Set to `false` to scan gitignored files too.

### `[detangle.lanes]` — table, default both `false`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `nli` | bool | `false` | Enable the local NLI cross-encoder lane (needs `detangle[nli]`) |
| `jury` | bool | `false` | Enable the LLM jury lane (needs `detangle[jury]` + `ANTHROPIC_API_KEY`) |

See [lanes.md](lanes.md). The `--nli` / `--jury` flags turn a lane **on** for one run; they
cannot turn off a lane enabled in the file. Must be a table, else a config error.

### `[detangle.rules]` — table, default empty

Per-rule enablement and severity overrides. Keys are rule codes (case-insensitive; unknown
codes are a config error). Values:

| Value | Effect |
|---|---|
| `false` or `"off"` | Disable the rule |
| `true` | Keep enabled (explicit no-op) |
| `"info"` / `"advisory"` / `"warning"` / `"error"` | Force this severity for every finding of the code |

Any other value is a config error. A severity override replaces the detector's own choice,
including dynamic downgrades (e.g. DTC01's exposure-based `error`→`warning`).

Scope note (current behavior): disables and severity overrides are applied to the
deterministic detectors' output. Findings emitted by the optional NLI/jury lanes are not
routed through this filter.

### `[detangle.jury]` — table

| Key | Type | Default | Meaning |
|---|---|---|---|
| `model` | string | `"claude-haiku-4-5-20251001"` | Anthropic model ID the jury uses (pin a snapshot, not an alias) |
| `max_pairs` | int | `200` | Cap on pairs adjudicated per run (2 API calls each) |

### `[detangle.nli]` — table

| key | type | default | meaning |
|---|---|---|---|
| `model` | string | `cross-encoder/nli-deberta-v3-small` | Hugging Face id of the NLI cross-encoder. Must have a 3-way (contradiction/entailment/neutral) head. |

### Not settable from TOML

These `Config` fields exist for programmatic/embedding use only:

- `root` — the scan root; comes from the CLI `path` argument.
- `user_dir` — a simulated `~` for user-global layers (tests/CI).
- `cache_dir` — verdict-cache location; defaults to `<root>/.detangle-cache/`.

---

## Suppression pragmas

Suppressions live *in the config files themselves*, as HTML comments, so they are visible in
the same review as the instructions they cover — and each one must say why.

```markdown
<!-- detangle-ignore DTC05: hotfix exception is intentional until Q3 -->
- Feel free to push directly to main for hotfixes.

<!-- detangle-ignore DTC01, DTP02: migrating this section next sprint -->

<!-- detangle-ignore-file DTR05: examples reference planned files -->
```

Syntax and semantics (exact):

- **Form:** `detangle-ignore` or `detangle-ignore-file`, followed by one or more rule codes
  (`DT[CPRSX]NN`, comma-separated, case-insensitive), optionally followed by `: reason`.
- **Line scope (`detangle-ignore`):** suppresses findings of those codes whose evidence spans
  **start within 6 lines below the pragma** (practically: the instruction(s) right under it),
  in the same file.
- **File scope (`detangle-ignore-file`):** suppresses those codes for the whole file. Can be
  placed anywhere in the file.
- **Justification is required:** a pragma without a `: reason` still suppresses, but is
  itself surfaced as a scan warning telling you to add one.
- A pairwise finding is suppressed if the pragma covers *either* of its evidence spans.
- Suppressed findings are removed from the reported findings (and therefore from exit-code
  computation) but kept, with the matching pragma and its reason, in the scan result's
  suppressed list.

---

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Scan completed; no finding at or above `fail_on`, and `conflict_budget` (if set) not exceeded. Also: `--version`, bare `detangle` (help), `rules`, successful `explain`. |
| `1` | At least one reported finding at or above `fail_on`, **or** more reported findings than `conflict_budget`. |
| `2` | Usage or configuration error: the path is not a directory, the config file is invalid, `--select` names an unknown rule code, `explain` got an unknown code, or the arguments do not parse. |

Notes: advisory/info findings never fail a run under the default `fail_on = "error"`; the
jury lane's `NEEDS_HUMAN` findings are info-level by design and can never fail CI.
`detangle diff` computes the exit code on the *filtered* finding set (only findings touching
changed files).

---

## CLI reference

```
detangle [--version] <command> [options]
```

### `detangle scan [path]`

Full scan of `path` (default: `.`).

| Flag | Meaning |
|---|---|
| `path` | Repository root to scan (positional, default `.`; must be a directory) |
| `--config PATH` | Explicit config file (skips discovery) |
| `--format {console,json,sarif,markdown}` | Output format (default `console`; `sarif` is SARIF 2.1.0 for GitHub code scanning) |
| `--output PATH`, `-o PATH` | Write the report to a file instead of stdout. Quirk: combined with the default `console` format, the file receives the JSON rendering (console output is TTY-only). |
| `--fail-on {info,advisory,warning,error}` | Override the config's `fail_on` for this run |
| `--nli` | Enable the NLI lane for this run |
| `--jury` | Enable the jury lane for this run |
| `--no-soft` | Hide advisory/info findings (sets `include_soft = false`) |
| `--select CODES` | Comma-separated rule codes to run **exclusively** (e.g. `DTC01,DTC03`). Unknown codes exit 2. Replaces any `[detangle.rules]` disables for the run. |
| `-v`, `--verbose` | More detail in console output |

### `detangle diff [path] [--base REF]`

Same flags as `scan`, plus:

| Flag | Meaning |
|---|---|
| `--base REF` | Git ref to diff against (default `origin/main`) |

Runs a full scan, then reports only findings that involve config files changed vs `--base`
(tried as `base...HEAD`, falling back to `git diff base`; if git fails entirely, a warning is
printed and *all* findings are reported). Use it in PR CI to gate only on newly introduced
tangles.

### `detangle explain CODE`

Prints the rule's name, default severity, summary, and a link to its section in
[taxonomy.md](taxonomy.md). Accepts a bare code (`DTP02`) or a finding fingerprint prefix
(`DTP02:9f3a…` — everything before the colon is used). Unknown codes exit 2.

### `detangle rules`

Lists every rule code with its name, default severity, and one-line summary.

---

## Environment variables

| Variable | Used by | Meaning |
|---|---|---|
| `ANTHROPIC_API_KEY` | jury lane only | API key for the Anthropic SDK. If missing while `--jury` is requested, the lane is skipped with a warning; the scan still completes. |

The deterministic core reads no environment variables and makes no network calls.
