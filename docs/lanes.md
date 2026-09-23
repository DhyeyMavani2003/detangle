# Analysis lanes

detangle has two tiers and an experimental add-on:

| Lane | Status | How to turn it on | Needs | Role |
|---|---|---|---|---|
| **Deterministic** | default, always on | nothing | nothing: zero network, zero LLM calls | Decides every rule it can: numeric, format and permit-vs-forbid clashes, duplicates, shadowing, precedence, size budgets, stale references, routing, hidden text |
| **TypeSafe** | recommended thorough pass | `--typesafe` or `lanes.typesafe = true` | `TYPESAFE_API_KEY`; sends instruction text to TypeSafe's hosted API | Calibrated typed judgments on instruction pairs; finds the semantic conflicts the deterministic rules cannot phrase |
| **Screen + jury** | experimental | `--screen` (implies `--jury`), or `--jury` alone | any LLM backend: the `claude` CLI, `ANTHROPIC_API_KEY`, or an OpenAI-compatible endpoint including local Ollama | A strong model nominates suspicious pairs from the whole config; a juror adjudicates them. The keyless-by-TypeSafe semantic option |

`--deep` turns on every lane that has a key or backend, for scheduled overnight scans
([triage.md](triage.md)). How the lanes compare on the benchmark, with the statistical
caveats, is in [benchmark.md](benchmark.md); the experiments behind the TypeSafe lane's
design are in [experiments.md](experiments.md).

The NLI cross-encoder lane of earlier versions was **removed**. It never emitted a finding,
it could not tell "conflicting" from merely "different" (both scored ~0.99), and it pulled in
about 4 GB of torch; the TypeSafe lane's calibrated band does its one job, pre-filtering the
jury's queue, better. Old configs and scripts that still say `nli` keep working: the key or
flag is ignored and the report's notes say so.

A planned **formal lane** (clingo/ASP + Z3 for the formalizable subset, with unsat-core
witnesses) is what the reserved codes DTC06 and DTC07 are waiting for. It does not exist yet.

---

## Lane 1: deterministic (always on)

Pure Python. Zero LLM calls, zero network, no ML dependencies. This lane does all discovery,
extraction, co-activation analysis, candidate blocking, and every detector described in
[taxonomy.md](taxonomy.md) except the reserved codes. It is built from curated lexicons
(imperative-strength ranking, antonym pairs, comparator phrases, unit tables, format families)
tuned precision-first: a missed entry costs recall (the optional lanes can recover it); a bad
entry costs a false positive.

**Determinism guarantee:** given the same repository content, the same detangle version, and
the same configuration, the deterministic lane produces byte-identical findings. There is no
randomness, no wall-clock dependence in results (timings in `stats` vary, findings do not),
and no environment dependence. Safe for CI gating and air-gapped repos. Deterministic findings
carry `"lanes": ["deterministic"]`; their `confidence` is 1.0 for exact frame, numeric and
scope clashes and a fixed lower value (0.6–0.9) for the heuristic classes — stale
references, drifted near-duplicates, description mismatch.

Numbers, in particular, stay here permanently: numeric and unit comparisons are routed to the
deterministic interval checker because language models are demonstrably weak on them (EQUATE),
and numeric mismatch is the largest real-world contradiction class (de Marneffe: 29%).

---

## Lane 2: TypeSafe (recommended thorough pass)

```bash
export TYPESAFE_API_KEY=...
detangle scan --typesafe
```

**What it is.** [TypeSafe](https://typesafe.ai) runs a hosted API that answers typed
questions with probabilities. For every pair of instruction units that can be loaded
together, detangle asks TypeSafe's Jev model one **Choice** question, *classify the
relationship*: contradictory, conditional conflict, numeric-limit conflict, format
conflict, permit-vs-forbid, order conflict, goal tension, redundant, or distinct. It reads
back a probability for each class. The jury asks a model to *write* a verdict and parses
the text; this lane gets numbers it can threshold.

**What leaves your machine.** The lane sends the text of your config's instruction units,
with their file, layer and activation metadata, to `https://api.typesafe.ai/v1/systemone`
(configurable). It needs a TypeSafe account key in the variable named by
`[detangle.typesafe] api_key_env`, default `TYPESAFE_API_KEY`. Without a key the lane is
skipped with a note and the scan finishes on the deterministic lane. The deterministic lane
sends nothing anywhere. In GitHub Actions, store the key as a repository secret (**Settings →
Secrets and variables → Actions**) and pass it to the scan step as an environment variable;
this repo's nightly and `hybrid-eval` workflows read a secret named `TYPESAFE_API_KEY`.

**How it decides.**

- **Both orderings, one request.** Every pair is asked as (A, B) and (B, A) against one
  shared `state` holding the config's units; `pairs_per_call` pairs (default 20) ride in one
  call. A finding needs the conflict mass (contradictory plus the other clash classes) to
  cross `tau` (default 0.7) in *both* orderings; the class is the argmax of the two
  orderings' mean distribution.
- **Two passes.** Judgment degrades as the shared state grows, so every pair the batched pass
  puts at or above `uncertain_low` (default 0.3) is re-asked alone. A pair fires only when
  both passes see the conflict; the solo answer decides clearing and the class
  (`rejudge = true`).
- **The question carries detangle's own account** of when the two units load together and
  what the ecosystem says about precedence, with a note that loading together is not by
  itself a clash.
- **Severity.** At or above `strong` (default 0.9) a verdict is a `warning`, below it
  `advisory`; conditional conflicts and goal tension always stay advisory, redundancy is
  advisory DTR01. Classes map to codes: numeric → DTC03, format → DTC04, permit-vs-forbid →
  DTC05, contradictory → DTC01, conditional and order conflicts → DTC02, goal tension →
  DTC08. A structural overlay mirrors the deterministic router: a skill or subagent body vs
  another layer is DTP04, two overlapping path-scoped rules are DTP02.
- **Hand-off.** With `--typesafe --jury`, pairs whose mass lands between `uncertain_low` and
  `tau` go to the jury; confident verdicts never do.

**How well it works.** On the 49-tree holdout it scores 27/30 strict, 27/30 class-lenient and
0/19 false positives in 13–20 s, the best row in [benchmark.md](benchmark.md). Two caveats
belong next to that number. The question wording, the class list, `tau` and the
co-activation note were each chosen by measuring on that same holdout, so 27/30 is an
optimistic estimate until a fresh test set exists. And on the realistic 134-unit demo agent
its precision is lower: of its 8 conflict findings, 4 are planted conflicts (C1, C3, C4,
C14) and 4 were triaged as not conflicts.

**What it misses.** It never asks about two skill *descriptions* competing for the same
trigger (DTS01) and has no drifted-duplicate class (DTR02); the deterministic lane covers
both. It has not flagged the demo agent's planted C2 ("it lints first" in CLAUDE.md vs "Run
`pnpm lint` last" in the pre-commit skill), although that conflict sits in a single sentence
pair that the experimental jury does flag; the one run that logged TypeSafe's score for that
pair put it at 0.17.

**Pair set and cost.** `pairs = "all"` (the default) asks every pair that can be loaded
together, which grows with the square of the config: about 8,000 pairs for a 134-unit
config. `pairs = "candidates"` asks only the deterministic lane's blocked pairs (2,665 for the
same config, 3–5 minutes and about 250 calls on a cold cache); use it above about 100 units.
Asking more pairs is not free recall. On the demo agent an all-pairs sweep (an older lane
version) judged three times the pairs for 6.2M input tokens and found fewer planted
conflicts than candidates mode (2 vs 4), and false alarms grow with the number of pairs
judged. Asking the same question again moves the answer by about ±0.01, so repeated
sampling buys nothing either.

**Cache and stability.** Verdicts are cached per pair by (linter version, model, prompt
hash, pair key) in `.detangle-cache/`, so re-scans of an unchanged config make zero calls.
The cache is also what keeps runs stable: a fresh judgment of the same question can move by
up to 0.09, so a pair within a few hundredths of `tau` can land on either side on a
different day. The nightly workflow persists the cache between nights for this reason.

**Failure behavior.** No key: the lane is skipped with a note. 429/5xx responses and dropped
connections are retried with exponential backoff. A malformed answer is never cached, and a
failed call leaves pairs unjudged and marks the lane incomplete, so the triage baseline does
not mistake an unjudged pair for a resolved one.

---

## Experimental: the LLM screen and jury

These two lanes run on **any** LLM backend: the Claude Code CLI you may already pay for, the
Anthropic API, or any OpenAI-compatible endpoint including a fully local Ollama server. That
makes them the semantic option for users without a TypeSafe key.

They are experimental because they cost more than they add on the benchmark. Under lenient
scoring they find the same 27 holdout conflicts as TypeSafe, but they label fewer correctly
(16–20/30 strict), add 2–4 advisory false positives, and take minutes to hours instead of
seconds. On the demo agent they are also the only lanes that found C2 and three real
conflicts nobody planted. Their extra findings arrive at advisory severity and need a human
answer, which is what the triage baseline is for ([triage.md](triage.md)).

### Jury: adjudication, not discovery

```bash
detangle scan --jury
detangle scan --typesafe --jury   # the jury gets only TypeSafe's uncertain band
```

The jury lane implements the jury protocol distilled from the LLM-as-judge reliability
literature. v0.1 ships a **single juror** (the protocol shape is the multi-juror one, so
additional jurors are additive later).

#### Backends

The juror is backend-agnostic — `[detangle.jury] backend` selects the transport
(default `"auto"`):

| backend | needs | default model | notes |
|---|---|---|---|
| `claude-cli` | the `claude` executable on PATH | `haiku` | **Zero-config**: `claude -p` print mode rides your existing Claude Code subscription. Runs in an empty scratch dir so the juror never ingests the scanned repo's own CLAUDE.md. Validated end-to-end in this repo. |
| `anthropic` | `detangle[jury]` + `ANTHROPIC_API_KEY` | `claude-haiku-4-5-20251001` | The Anthropic API; pin snapshots. An organization-level key (not created inside a workspace) also needs `ANTHROPIC_WORKSPACE_ID`; a workspace-scoped key does not. |
| `openai` | `[detangle.jury] base_url` (+ optional key via `api_key_env`) | `gpt-5-mini` | Any OpenAI-compatible `/chat/completions` endpoint — OpenAI, DeepSeek, Gemini's compat layer, **Ollama/vLLM for fully-local juries** (`base_url = "http://localhost:11434/v1"`, no key). Stdlib urllib; zero extra dependencies. |

`auto` picks the first available: `ANTHROPIC_API_KEY` → anthropic, else `claude` on
PATH → claude-cli, else a configured `base_url` → openai, else the lane skips with a note.
The backend and model are part of the verdict-cache key, so switching either invalidates
cached verdicts — never silently mixes them.

```toml
[detangle.jury]
backend = "openai"                      # or "claude-cli" / "anthropic" / "auto"
base_url = "http://localhost:11434/v1"  # openai backend only
api_key_env = "OPENAI_API_KEY"          # openai backend only; unset env = no auth header
model = "qwen3:8b"
```

#### Protocol summary

1. **Adjudicate-only.** The judge never reads raw config files hunting for conflicts —
   open-ended LLM detection collapses (GPT-4 whole-document contradiction judgment: 53.8%
   accuracy, 8% recall; verify-given-candidates evidence hit rate: 92.7%). It only classifies
   candidate pairs the deterministic pipeline extracted.
2. **Neutral framing.** The prompt says *"classify the relationship"* — never "we suspect
   these conflict, confirm" (sycophancy). The prompt explicitly states most pairs do not
   conflict.
3. **Evidence before verdict.** The JSON schema is field-order-constrained:
   `overlap_condition`, `evidence_a`, `evidence_b`, `reasoning_summary` (≤40 words), and only
   *then* `verdict`, `conflict_type`, `resolution_hint`, `confidence`. Field order measurably
   matters; long chain-of-thought before the verdict is deliberately avoided (it hurts
   calibration in most scoring configurations).
4. **Order swap.** Every pair is judged twice — as (A, B) and as (B, A). If the two verdicts
   differ, the juror **abstains** on that pair (position bias bites hardest on exactly the
   marginal calls a linter sees).
5. **Evidence validation.** The returned `evidence_a`/`evidence_b` quotes must actually occur
   in the source texts (with a similarity fallback for light re-punctuation). A verdict with
   fabricated evidence is rejected → abstention.
6. **NEEDS_HUMAN.** Every abstention (unparseable output, order instability, bad evidence)
   surfaces as an `info`-level DTC02 finding tagged `needs-human` — visible, but **never** a
   CI-failing severity.
7. **Caching.** Verdicts are cached in `.detangle-cache/verdicts.json` (configurable via
   `Config.cache_dir`), keyed by detangle version, model ID, prompt hash, and the
   order-independent pair key plus the ordering policy (`swap-both`). Re-running an unchanged
   repo makes **zero** API calls; changing the prompt, the model, the linter version, or
   either instruction misses the cache. Committing or CI-caching the cache file makes jury
   runs reproducible and free.

#### Verdicts and how they map to findings

The verdict enum: `CONTRADICTORY`, `CONDITIONAL_CONFLICT`, `PRECEDENCE_RESOLVED`, `REDUNDANT`,
`DISTINCT`; conflict types: `negation`, `unsatisfiable_constraint`, `temporal`, `numeric`,
`specificity`, `authority`, `process`, `none`.

| Verdict | Result |
|---|---|
| `CONTRADICTORY` | DTC01 finding at `warning`, `lanes: ["jury"]` |
| `CONDITIONAL_CONFLICT` | DTC02 finding at `advisory` (DTC03 when `conflict_type` is `numeric`; DTP03 when exactly one side is a deliberate carve-out), with the model's `overlap_condition` as the witness |
| `REDUNDANT` | DTR01 finding at `advisory` |
| `PRECEDENCE_RESOLVED` | no finding (declared hierarchy resolves it) |
| `DISTINCT` | no finding |

Jury findings are capped at `warning`: a single-juror LLM verdict is never allowed to be the
sole source of an `error`.

#### Candidate selection

- Screen-lane nominations come **first** when `--screen` is on (a strong model chose them by
  reading the whole config, including pairs blocking could never form).
- Then the TypeSafe lane's uncertain band (`[uncertain_low, tau)`), best-scored first, when
  `--typesafe` ran to completion; pairs TypeSafe cleared are never adjudicated.
- Otherwise: unclaimed pairs ranked by lexical similarity, highest first.
- Hard cap: `jury_max_pairs` (default **200** pairs) — the budget valve.

#### Cost expectations

Each adjudicated pair costs **two** API requests (the order swap), each roughly 500–900 input
tokens and ≤400 output tokens. With the default cap of 200 pairs and Claude Haiku 4.5 pricing
($1/M input, $5/M output at the time of writing — re-verify current pricing), a full cold run
is on the order of **well under one US dollar**; the research cost model for this tier is
$2–8 per 1,000 candidate pairs (versus $25–40 per 1,000 for a frontier-everything baseline).
Warm runs cost nothing for unchanged pairs thanks to the verdict cache. Batch APIs and prompt
caching (the shared rubric is a fixed system prompt) can cut cold-run cost further; the
shipped lane issues plain synchronous calls.

#### Determinism

**Determinism is protocol-engineered, never assumed.** Temperature is set to 0 wherever the
transport still accepts one (the 1.x Anthropic SDK, the Claude 5 API generation, has no
sampling parameters at all; the backend sends `temperature` only when the installed SDK's
`messages.create` accepts it), but temp-0 API calls are still nondeterministic in general
(batch non-invariance; 1,000 greedy completions have been observed to yield 80 distinct
outputs). What actually makes jury results stable:

- the closed verdict **enum** (no free-text judgments),
- the **order-swap + abstain** rule (order-sensitive verdicts become NEEDS_HUMAN instead of
  flipping between runs),
- **evidence validation** (hallucinated support is rejected rather than trusted), and
- the **verdict cache** (a pair, once adjudicated, keeps its verdict until the version, model,
  prompt, or text changes).

Pin the model snapshot in `[detangle.jury]` and treat any model migration as a calibration
event — model aliases drift (a documented case degraded 84% → 51% in three months).

#### Failure behavior

No available backend (no key, no CLI, no base_url) skips the lane with a warning note;
the scan still completes. API keys are read from the environment only — never written to
disk, and never required for the deterministic lane. Transient backend failures
(network, CLI errors) are never cached and never produce findings; after three such
failures in a run the lane aborts with a note.

```bash
# local, API backend
export ANTHROPIC_API_KEY=sk-ant-...

# local, subscription backend: nothing to set — just have Claude Code installed

# GitHub Actions (API backend); ANTHROPIC_WORKSPACE_ID only for an
# organization-level key — a workspace-scoped key needs the key alone
env:
  ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
  ANTHROPIC_WORKSPACE_ID: ${{ secrets.ANTHROPIC_WORKSPACE_ID }}
```

---

### Screen: nomination, not verdicts

```bash
detangle scan --screen           # implies --jury
```

The deterministic lane's recall ceiling is **candidate formation**: a conflict whose phrasing
defeats the lexicons never becomes a candidate pair, so no downstream judge ever sees it. The
screen lane attacks exactly that. A strong model reads **every** extracted unit — including
the weak, hedged sentences the precision-first classifier normally rejects (high-recall
extraction is switched on automatically with `--screen`; the deterministic detectors ignore
those weak units) — together with file, layer (always-on memory vs conditionally loaded
skill/rule body), and activation metadata, and nominates suspicious pairs across the classes
only whole-config reasoning can see:

- **procedural/order conflicts** — step A-before-B vs B-before-A, and skill-orchestration
  order: an always-on file prescribing a skill-invocation sequence that a skill's own body
  contradicts;
- **cross-layer conflicts** — the always-on CLAUDE.md/AGENTS.md vs the conditionally-loaded
  skill bodies that join the context when a skill fires (the prompt tells the screen
  explicitly that a skill's body activates *together with* the main files);
- hedged/colloquial contradictions, numeric and format clashes phrased outside the
  deterministic vocabulary, and semantic redundancy.

**Nominations are not findings.** Every nominated pair goes through the jury's full
swap-validated adjudication protocol (both orderings, evidence validation, verdict enums) —
the screen buys recall, the jury keeps precision. This is the research's group-screen →
pair-judge cascade: open-ended whole-document *verdicts* collapse (8% recall, 53.8%
accuracy), but whole-document *nomination* feeding a pair-level judge is the configuration
that works.

**Deep multi-sweep (`--deep`).** One generic sweep asks a single call to spot every class at
once; under `--deep` the screen instead runs **ten** passes — the generic sweep plus one
focused, single-lens sweep per conflict class (order, cross-layer, numeric, format,
permit-forbid, redundancy, tension, contradiction, conditional). Each sweep reads every unit
with one question in mind; the union of nominations goes to the jury. This is the
thoroughness-first mode built for overnight CI — see [triage.md](triage.md).

**Cost & chunking.** One screen call covers up to 150 units; larger configs are chunked with
every always-on unit repeated in every chunk (so main-file-vs-skill pairs survive chunking).
Screen calls are cached by (backend, model, prompt hash, unit-set hash) — re-screening an
unchanged config is free. Use a strong model here: the screen is one call doing whole-config
reasoning, so this is where model quality pays. Defaults per backend: `claude-cli` → `opus`,
`anthropic` → `claude-opus-5`, `openai` → `gpt-5`; override with `[detangle.screen] model`.
The jury that adjudicates the nominations can stay on a cheaper model — a
screen-with-frontier-model + jury-with-mid-tier split is the intended shape.

**Failure behavior.** No available backend skips the lane with a note (the scan completes on
whatever lanes remain); a failed screen call marks the sweep incomplete but keeps the scan
alive. Nominations that target the same span, are mutually exclusive by activation, or are
already claimed by a deterministic detector are dropped before adjudication.

Findings that originate from a screen nomination carry `lanes: ["jury", "screen"]` — the
verdict is always the jury's.

---

## Which lane decided what?

Every finding carries a `lanes` array: `["deterministic"]`, `["typesafe"]` (a calibrated
typed verdict; its `confidence` is the conflict probability), `["jury"]` (an adjudicated
verdict), or `["jury", "screen"]` (a screen nomination the jury upheld), plus a `confidence`
in [0, 1]: deterministic findings are 1.0 for the exact classes and 0.6–0.9 for the
heuristic ones, lane findings carry the lane's own confidence. CI policy can key off severity
alone (the default), since lane-sourced findings already encode their reliability in the
severity they are allowed to use.
