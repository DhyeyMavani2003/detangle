# Analysis lanes

detangle runs up to three analysis lanes. The design follows the contradiction-detection
literature's cascade result: deterministic rules for what rules do best (negation, antonymy,
numbers, scopes), an NLI cross-encoder as a *recall filter*, and an LLM judge as the only tier
allowed to issue semantic verdicts. Each lane is honest about what it can and cannot do.

| Lane | Enabled | Cost | Network | Role |
|---|---|---|---|---|
| **Deterministic** | always | free | none | The verdict-giver for everything it can decide |
| **NLI** | opt-in (`--nli` / `lanes.nli = true`) | local CPU/GPU inference | model download on first run | Recall filter and confidence signal — never a verdict-giver |
| **Jury** | opt-in (`--jury` / `lanes.jury = true`) | Anthropic API calls | yes | Schema-constrained adjudication of pre-extracted candidate pairs |

A planned fourth lane — the **formal lane** (clingo/ASP + Z3 for the formalizable subset, with
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
carry `"lanes": ["deterministic"]` and `confidence: 1.0`.

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

- **Standalone (`--nli` without `--jury`):**
  - Pairs scoring at or above the strict auto-flag threshold that the deterministic lane
    *already* flagged get `"nli"` added to their `lanes` — two independent lanes agreeing.
  - Pairs above the threshold that the deterministic lane could *not* confirm are surfaced as
    DTC01 findings at `warning` severity, worded explicitly as *"a lead, not a verdict"*,
    with reduced confidence.
- **With the jury (`--nli --jury`):** the NLI scores become a banding function
  (Fellegi–Sunter style): the auto-flag band and the gray zone are handed to the jury for
  adjudication instead of being reported directly.

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

**Banding thresholds.** Symmetrized contradiction probability is banded at `TAU_LOW = 0.25`
(gray-zone floor) and `TAU_HIGH = 0.88` (auto-flag). These were calibrated on the seeded
benchmark in `benchmarks/`, consistent with the research warning that raw NLI softmax is
miscalibrated out-of-domain and must be treated as a ranking, calibrated on labeled pairs,
before any absolute threshold is trusted.

**Guards baked in:**

- **Symmetrization:** every pair is scored in both orders — (A premise, B hypothesis) and
  (B, A) — and the max contradiction probability is used.
- **Negation-bias guard:** NLI models over-predict contradiction when negation words are
  present (dataset artifacts; Poliak 2018, Hossain 2020). Pairs where *both* sides are
  negation-dense get their confidence cut (0.7 → 0.55) when surfaced standalone.
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
pip install 'detangle[jury]'     # the anthropic SDK
export ANTHROPIC_API_KEY=sk-ant-...
detangle scan --jury             # or --nli --jury for banded candidates
```

The jury lane implements the jury protocol distilled from the LLM-as-judge reliability
literature. v0.1 ships a **single juror** (the protocol shape is the multi-juror one, so
additional jurors are additive later). Default model: `claude-haiku-4-5-20251001` — a pinned
snapshot, configurable via `[detangle.jury] model`.

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
| `CONDITIONAL_CONFLICT` | DTC02 finding at `warning` (with the model's `overlap_condition` as the witness) |
| `REDUNDANT` | DTR01 finding at `advisory` |
| `PRECEDENCE_RESOLVED` | no finding (declared hierarchy resolves it) |
| `DISTINCT` | no finding |

Jury findings are capped at `warning`: a single-juror LLM verdict is never allowed to be the
sole source of an `error`.

### Candidate selection

- If the NLI lane ran: the auto-flag band plus the gray zone, in that order.
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

**Determinism is protocol-engineered, never assumed.** Temperature is set to 0, but temp-0 API
calls are still nondeterministic in general (batch non-invariance; 1,000 greedy completions
have been observed to yield 80 distinct outputs). What actually makes jury results stable:

- the closed verdict **enum** (no free-text judgments),
- the **order-swap + abstain** rule (order-sensitive verdicts become NEEDS_HUMAN instead of
  flipping between runs),
- **evidence validation** (hallucinated support is rejected rather than trusted), and
- the **verdict cache** (a pair, once adjudicated, keeps its verdict until the version, model,
  prompt, or text changes).

Pin the model snapshot in `[detangle.jury]` and treat any model migration as a calibration
event — model aliases drift (a documented case degraded 84% → 51% in three months).

### Failure behavior

Missing `anthropic` package or missing `ANTHROPIC_API_KEY` skips the lane with a warning note;
the scan still completes. The key is read from the environment only — it is never written to
disk, and never required for the deterministic or NLI lanes.

```bash
# local
export ANTHROPIC_API_KEY=sk-ant-...

# GitHub Actions
env:
  ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
```

---

## Which lane decided what?

Every finding carries a `lanes` array: `["deterministic"]`, `["deterministic", "nli"]` (two
lanes agree), `["nli"]` (a lead), or `["jury"]` (an adjudicated verdict), plus a `confidence`
in [0, 1] — deterministic findings are 1.0, lane findings carry the lane's own confidence. CI
policy can key off severity alone (the default), since lane-sourced findings already encode
their reliability in the severity they are allowed to use.
