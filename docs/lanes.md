# Analysis lanes

detangle runs up to five analysis lanes. The design follows the contradiction-detection
literature's cascade result: deterministic rules for what rules do best (negation, antonymy,
numbers, scopes), an NLI cross-encoder as a *recall filter*, a strong-model **screen** as a
whole-config *nominator*, an LLM jury that issues schema-constrained semantic verdicts on
candidate pairs, and a TypeSafe lane that issues calibrated typed verdicts with
probabilities (the jury and TypeSafe are the verdict-givers; NLI and the screen never are).
Each lane is honest about what it can and cannot do.

| Lane | Enabled | Cost | Network | Role |
|---|---|---|---|---|
| **Deterministic** | always | free | none | The verdict-giver for everything it can decide |
| **NLI** | opt-in (`--nli` / `lanes.nli = true`) | local CPU/GPU inference | model download on first run | Recall filter and confidence signal — never a verdict-giver |
| **Screen** | opt-in (`--screen` / `lanes.screen = true`; implies jury) | one strong-model call per ~150 units | yes | Whole-config sweep that *nominates* suspicious pairs for the jury — never a verdict-giver |
| **Jury** | opt-in (`--jury` / `lanes.jury = true`) | LLM API calls | yes | Schema-constrained adjudication of pre-extracted candidate pairs |
| **TypeSafe** | opt-in (`--typesafe` / `lanes.typesafe = true`; `TYPESAFE_API_KEY`) | `pairs_per_call` (default 20) pairs per call, each in both orderings, plus one small call per band pair — seconds for small trees, 3–5 min for a realistic config on a cold cache (demo agent: 2,665 pairs, ~250 calls), free on re-scan | yes | Calibrated typed pair judgments: a verdict-giver with probabilities, thresholded — hands its uncertain band to the jury |

A planned **formal lane** (clingo/ASP + Z3 for the formalizable subset, with
unsat-core witnesses) — is what the reserved codes DTC06 and DTC07 are waiting for. It does not
exist yet.

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
deterministic interval checker because NLI models are demonstrably weak on them (EQUATE), and
numeric mismatch is the largest real-world contradiction class (de Marneffe: 29%).

---

## Lane 2: NLI (optional) — a filter, not a judge

```bash
pip install 'detangle[nli]'      # sentence-transformers + torch
detangle scan --nli
```

The lane scores every *unclaimed* candidate pair (pairs the deterministic detectors did not
already explain) with a cross-encoder NLI model and uses the symmetrized contradiction
probability.

**Honest expectations, up front:** on norm-pair benchmarks, NLI-only contradiction detection
runs at roughly **37% precision** (81.6% recall) — LegalWiz/ContraGen measured NLI-only at
37.3%/81.6% versus 92% accuracy for the hybrid NLI-filter → LLM-judge cascade. MNLI-range
accuracy (~90%) does not transfer to instruction pairs, which are out-of-domain; ANLI-range
(55–70%) is the realistic ceiling. **That is why this lane is a filter.** detangle never ships
raw NLI verdicts as errors:

**Measured on this codebase (2026-08-30):** we ran the shipped model over declarativized
instruction pairs and A/B-tested three normalization templates, per the research's advice.
The result is stark: true contradictions score ~1.00 — but so do pairs of merely *different*
prescriptions ("must run tests" vs "must write documentation" scores 0.99 contradiction under
every template). This is the single-event NLI artifact: the model reads two different
obligations about one subject as incompatible. Paraphrases and benign specializations,
by contrast, reliably score ~0.00. So for this model class only one band exists:

- **Auto-clear** (symmetrized contradiction < `TAU_CLEAR = 0.25`): the pair is definitively
  compatible.
- Everything else is merely *not cleared* — a high score cannot distinguish "conflicting"
  from "different".

detangle's lane semantics follow the measurement:

- **Standalone (`--nli` without `--jury`):** the lane reports how many pairs it auto-cleared
  (a scan note) and **never emits a finding from an NLI score alone**.
- **With the jury (`--nli --jury`):** auto-cleared pairs are never sent to the jury (a
  cost saver — compatible pairs skip adjudication entirely); the not-cleared band is handed
  to the jury ranked by score.

**Model.** Configurable via `[detangle.nli] model = "..."` in `.detangle.toml`.
Default: `cross-encoder/nli-deberta-v3-small` (0.1B — the cheap pre-filter tier;
SNLI 91.65, MNLI-mm 87.55). A 3-way (contradiction/entailment/neutral) head is required;
binary entail/not-entail models collapse the contradiction class and must not be substituted.
The research shortlist for stronger setups, in order:

1. `MoritzLaurer/DeBERTa-v3-large-mnli-fever-anli-ling-wanli` — 0.4B, MIT; the
   most adversarially robust of the classic generation (ANLI-all 70.2).
2. `dleemiller/ModernCE-large-nli` — 395M, MIT; ~540 pairs/s; useful as an ensemble
   companion where disagreement is an uncertainty signal.
3. `tasksource/deberta-base-long-nli` — 1280-token context, for whole-section premises.

Avoid `facebook/bart-large-mnli` (outdated HF zero-shot default) and the
`deberta-v3-large-zeroshot-v2.0` family (binary head). To swap in a substitute, set
`[detangle.nli] model = "..."` in `.detangle.toml`
(see [docs/configuration.md](configuration.md)).

**Banding threshold.** `TAU_CLEAR = 0.25` on the symmetrized contradiction probability.
The measured bands are far apart (compatible pairs ~0.00, everything else ~0.99), so the
exact value is uncritical; it errs toward sending pairs to the jury. Raw NLI softmax remains
miscalibrated out-of-domain — treat scores as a ranking, never as probabilities.

**Guards baked in:**

- **Symmetrization:** every pair is scored in both orders — (A premise, B hypothesis) and
  (B, A) — and the max contradiction probability is used.
- **Negation bias:** NLI models over-predict contradiction when negation words are
  present (dataset artifacts; Poliak 2018, Hossain 2020) — one more reason a high score is
  never treated as a verdict; only the low-score auto-clear band is used.
- **Overlap guard (HANS):** high lexical overlap must not imply conflict. This is handled by
  detector ordering — the deterministic conflict router and duplicate detector claim pairs
  first, so wordy paraphrases are classified as redundancy, not contradiction.
- **Declarativization:** units are compared via their normalized declarative form (imperatives
  lack truth values; NLI models are trained on declaratives).

**Failure behavior:** if `sentence-transformers` is not installed, or the model fails to
download/load, the lane is skipped with a warning note — the scan still completes on the
deterministic lane.

**Determinism:** inference is deterministic for a fixed model snapshot, library versions, and
hardware; scores can differ in the last decimal places across hardware/framework builds. The
lane does not introduce ordering nondeterminism (pairs are scored in a fixed order), but
bit-identical output across *different* machines is not guaranteed the way it is for the
deterministic lane.

---

## Lane 3: jury (optional) — adjudication, not discovery

```bash
detangle scan --jury             # or --nli --jury for banded candidates
```

The jury lane implements the jury protocol distilled from the LLM-as-judge reliability
literature. v0.1 ships a **single juror** (the protocol shape is the multi-juror one, so
additional jurors are additive later).

### Backends

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

### Protocol summary

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

### Verdicts and how they map to findings

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

### Candidate selection

- Screen-lane nominations come **first** when `--screen` is on (a strong model chose them by
  reading the whole config, including pairs blocking could never form).
- Then the uncertain bands handed on by the calibrated lanes, best-scored first and merged
  by pair: the TypeSafe lane's `[uncertain_low, tau)` band and, if the NLI lane ran, its
  not-cleared band (auto-cleared pairs, and pairs TypeSafe cleared, are never adjudicated).
- Otherwise: unclaimed pairs ranked by lexical similarity, highest first.
- Hard cap: `jury_max_pairs` (default **200** pairs) — the budget valve.

### Cost expectations

Each adjudicated pair costs **two** API requests (the order swap), each roughly 500–900 input
tokens and ≤400 output tokens. With the default cap of 200 pairs and Claude Haiku 4.5 pricing
($1/M input, $5/M output at the time of writing — re-verify current pricing), a full cold run
is on the order of **well under one US dollar**; the research cost model for this tier is
$2–8 per 1,000 candidate pairs (versus $25–40 per 1,000 for a frontier-everything baseline).
Warm runs cost nothing for unchanged pairs thanks to the verdict cache. Batch APIs and prompt
caching (the shared rubric is a fixed system prompt) can cut cold-run cost further; the
shipped lane issues plain synchronous calls.

### Determinism

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

### Measured results (live validation)

The full cascade was validated end-to-end in this repository on the novel-phrasing holdout
— 30 conflict + 19 benign trees — with the `claude-cli` backend for the LLM rows
(2026-08-31; `python -m benchmarks.run_eval --holdout --lanes ... --jury-model ...
--screen-model ...`), against the TypeSafe API for the TypeSafe row (2026-09-17;
`--lanes typesafe` with `TYPESAFE_API_KEY` set; the `hybrid-eval` workflow reproduced it on a
GitHub runner in 8 s), and through the Anthropic API for the rows marked so (2026-09-17; the
`hybrid-eval` workflow on GitHub runners with `ANTHROPIC_API_KEY` set — `anthropic` backend,
`claude-opus-5` screen, `claude-haiku-4-5-20251001` or `claude-opus-5` jury):

| configuration | strict recall | class-lenient | FPs (all advisory-tier) |
|---|---|---|---|
| deterministic only | 5/30 (17%) | 5/30 (17%) | 0/19 |
| + NLI auto-clear (no jury) | 5/30 (17%) | 5/30 (17%) | 0/19 |
| NLI + jury (`haiku`) | 8/30 (27%) | 10/30 (33%) | 1/19 |
| NLI + jury (`sonnet`) | 7/30 (23%) | 11/30 (37%) | 2/19 |
| NLI + screen (`opus`) + jury (`sonnet`) | 17/30 (57%) | **27/30 (90%)** | 4/19 |
| NLI + screen (`opus`) + jury (`opus`) | 20/30 (67%) | **27/30 (90%)** | 2/19 |
| TypeSafe lane (`--typesafe`, ~20 s total) | **27/30 (90%)** | 27/30 (90%) | **0/19** |
| screen (`opus`) + jury (`haiku`) — Anthropic API, ~5 min | 16/30 (53%) | **27/30 (90%)** | 4/19 |
| screen (`opus`) + jury (`opus`) — Anthropic API, ~10 min | 19/30 (63%) | **27/30 (90%)** | 3/19 |
| TypeSafe + jury (`haiku`) — Anthropic API, ~20 s | **27/30 (90%)** | 27/30 (90%) | 1/19 |
| TypeSafe + screen (`opus`) + jury (`haiku`) — Anthropic API, ~3 min | **27/30 (90%)** | 27/30 (90%) | 3/19 |

The API rows reproduce the CLI rows within a few cases (the backends differ in model
snapshot and transport, and jury conditional verdicts are the noisiest band), and they show
that stacking the LLM cascade on the TypeSafe lane adds no holdout recall — only advisory
conditional-conflict findings on benign trees, from the jury adjudicating TypeSafe's
uncertain band (1/19) or the screen's nominations (3/19).

Two structural lessons in that table. First, a pair-level jury plateaus at ~a third of
conflicts regardless of juror strength — the bottleneck is candidate formation, which is
what the screen lane fixes (the screen configurations were the first to catch the
procedural/skill-ordering class — 4/4 strict with the `opus` jury; the TypeSafe lane, last
row, catches the same 4/4 from pair questions). Second, every measured false
positive in every configuration was a CONDITIONAL_CONFLICT verdict — which is why jury
conditional-conflict findings are emitted at **advisory** severity (never CI-failing),
while jury CONTRADICTORY findings are warnings. The verdict cache made repeat scans free
(3m22s → 8.8s on the seeded fixture) and byte-identical.
Verdicts whose ``conflict_type`` is ``numeric`` surface as DTC03; other CONTRADICTORY
verdicts as DTC01; CONDITIONAL_CONFLICT as DTC02 (or DTP03 when exactly one side is a
deliberate carve-out — the deterministic router's rule, applied consistently).

### Failure behavior

No available backend (no key, no CLI, no base_url) skips the lane with a warning note;
the scan still completes. API keys are read from the environment only — never written to
disk, and never required for the deterministic or NLI lanes. Transient backend failures
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

## Lane 4: screen (optional) — nomination, not verdicts

```bash
detangle scan --screen           # implies --jury; or --nli --screen for the full cascade
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

## Lane 5: TypeSafe (optional) — calibrated typed judgments

```bash
export TYPESAFE_API_KEY=...
detangle scan --typesafe            # TypeSafe adjudicates; confident verdicts become findings
detangle scan --typesafe --jury     # ...and the uncertain band goes to a generative juror
```

The jury asks a generative model to *write* a verdict and parses it. This lane instead asks
a System One decision model (TypeSafe's Jev) one typed **Choice** question per instruction
pair — *classify the relationship: contradictory / conditional conflict / numeric-limit
conflict / format conflict / permit-vs-forbid / order conflict / goal tension / redundant /
distinct* — and reads back a probability distribution over those classes. Two properties
make it a different kind of lane:

- **Batching.** Every question in a request is evaluated in parallel against one shared
  `state` (the config's units with file/layer/activation metadata), so a batch of
  `pairs_per_call` pairs (default 20; the request budget caps a batch at roughly 30) — each
  asked in **both orderings** — rides in one call. The 49-tree holdout adjudicates in about
  20 seconds; the deep opus+opus cascade took hours.
- **Calibration.** Emission is thresholded on the returned probabilities
  (`[detangle.typesafe] tau`, default 0.7; at or above `strong`, 0.9, a verdict is a
  `warning` — except conditional conflicts and goal tension, which stay advisory just as the
  jury's conditional verdicts do), and the
  conflict mass used is the *minimum* across the two orderings — the jury's order-swap
  guard at no extra round trip — and the class is the argmax of the two orderings' mean
  distribution (single-ordering argmaxes flip on near-ties; the mean does not).
  `numeric_limit_conflict` → DTC03, `format_conflict` → DTC04, `permit_vs_forbid` → DTC05,
  `contradictory` → DTC01, `conditional_conflict` and `order_conflict` → DTC02,
  `goal_tension` → DTC08, `redundant` → DTR01 (advisory); a structural overlay then mirrors
  the deterministic router — a conditionally-loaded layer (skill, subagent) vs another
  layer is DTP04, two overlapping path-scoped rules DTP02.
- **The question carries the co-activation account.** Each relation question includes
  detangle's own account of *when* the two units are loaded together — co-activation class
  and account, precedence kind and account from the activation model — with a note that
  being loaded together is not by itself a clash. Measured: +1 holdout case (the
  overlapping-glob precedence pair went from 0.45 to 0.96) on top of the rich vocabulary,
  +3 with a 4-option ablation vocabulary in the prompt ablation, 0 false positives, ~1.5×
  the question tokens. Structured criteria objects, an inverted "can it comply with both"
  framing, a statement framing and a 4-level severity Score question were also measured;
  none beat it.

**Measured (2026-09-17, novel-phrasing holdout, `python -m benchmarks.run_eval --holdout
--lanes typesafe`):** **27/30 strict (90%), 27/30 class-lenient (90%), 0/19 false
positives** in ~20 s — the opus-screen + opus-jury cascade's lenient recall (27/30) with 0
instead of 2 false positives and seven more strict hits than its 20/30, at a fraction of
the cost, and with **zero false positives at every threshold tried** (0.5–0.95) across every
question-style ablation. (Re-measured through the Anthropic API on 2026-09-17, the opus
screen + opus jury cascade lands at 19/30 strict, 27/30 lenient, 3/19 advisory false
positives in ~10 min.) The three misses are structurally outside a pair question: two
skill-routing-ambiguity cases (DTS01 lives in trigger descriptions) and a drifted
near-duplicate (DTR02).

Two design facts the experiments established:

- **Extraction is the ceiling, not judgment.** With the precision-first extractor's strict
  units the same lane reaches 11/30; with high-recall extraction (switched on
  automatically, like the screen) 19–27/30. The dropped sentences are exactly the
  procedural ones ("run the db-migrate skill to completion before starting the deploy
  skill") — TypeSafe's own is-instruction judgment flags them at p≥0.9.
- **Pair set.** `pairs = "all"` judges every co-activatable unit pair (O(n²): ~8,000 pairs
  for a 134-unit config, ~6M input tokens) and adds ~4 holdout cases over
  `pairs = "candidates"` (the deterministic lane's blocked pairs after the lane's high-recall
  extraction: 2,665 for the same config, not the ~1,100 a plain deterministic scan reports).
  Default is `all`; switch to `candidates` above ~100 units.
- **Two passes.** Judgment degrades as the shared state grows (a planted demo pair scores
  0.94 alone vs 0.38 inside a 95-pair batch), so after the batched pass every pair at or
  above `uncertain_low` is re-asked *alone* — a two-unit state, the same two questions
  (`rejudge = true`). The solo verdict decides clearing and the class; a pair **fires only
  when both passes see the conflict**. Measured on the demo agent (2,665 candidate pairs,
  116 re-asked in 116 small calls, ~50 s): 55 of the 109 band pairs cleared, so the jury
  receives half the pairs; the four planted pairs the lane owns stayed emitted, and one
  batched emit a human had triaged as not a conflict dropped to the band. The cost: one of
  the 55 cleared pairs was a soft goal-tension pair a human had marked `open` (the jury
  would otherwise have seen it) — set `rejudge = false` to hand the whole band on. Letting
  the solo verdict *replace* the batched one was measured and rejected: it promoted ten
  borderline pairs, three of which a human had already triaged as not conflicts, for one
  genuine emergent conflict — which the jury finds from the band anyway.

**What it cannot do.** A pair question sees two sentences. Conflicts carried by *list
position* across several lines (a skill whose steps are ordered tests → typecheck → lint
with no single sentence saying "lint last") need the whole-config screen; in the demo agent
the lane catches 4 of the 5 planted conflicts the deterministic lane doesn't already own,
missing exactly that one. Routing ambiguity (DTS01) stays deterministic.

**Cache & cost.** Verdicts cache per pair by (version, model, prompt hash, pair key) —
re-scans are free, and the cache is also what keeps runs *stable*: a fresh judgment of the
same question moves by ±0.01 (median; 0.09 at most), so a pair sitting within a few
hundredths of `tau` can land on either side on a different day or machine (the nightly
workflow persists the cache between nights for exactly this reason; the one finding a
fresh GitHub runner produced beyond the committed demo baseline was a 0.70 verdict). Failure
behavior mirrors the jury: no key → lane skipped with a note; 429/5xx and dropped
connections → exponential backoff; a malformed answer is never cached; a failed call leaves
pairs unjudged and marks the lane incomplete for the baseline's missing-stamp logic.

**Extractor audit (measured, not landed).** Per-unit TypeSafe questions make a sharp
audit of the deterministic extractor: on the demo agent it drops 40/134 directives (29.9%)
and mis-reads modality on 7/75 strict units (9.3%) — `can` inside a purpose clause ("so the
author can triage") read as a permission, a trailing "never the other way around" inverting
an imperative, prohibitions and obligations phrased as "No drive-by refactors" / "needs a
changeset", imperatives behind `Label:` prefixes and leading condition clauses. Feeding
those answers back into the deterministic detectors was measured at +1/30 on the holdout
(frames, not modality, are the binding constraint), and five adversarial review rounds
could not make any of the corresponding lexicon fixes precise: each traded a corrected
modality for a new false positive, a severity escalation or a lost finding on realistic
prose. Precision-first, none of them shipped; TypeSafe's value stays at the pair level.

---

## Which lane decided what?

Every finding carries a `lanes` array: `["deterministic"]`, `["jury"]` (an adjudicated
verdict), `["jury", "screen"]` (a screen nomination the jury upheld), or `["typesafe"]` (a
calibrated typed verdict; its `confidence` is the conflict probability), plus a `confidence`
in [0, 1] — deterministic findings are 1.0 for the exact classes and 0.6–0.9 for the
heuristic ones, lane findings carry the lane's own confidence. The NLI lane never appears in
`lanes`: it only clears pairs and ranks the jury's queue. CI
policy can key off severity alone (the default), since lane-sourced findings already encode
their reliability in the severity they are allowed to use.
