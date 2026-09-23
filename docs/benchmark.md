# Benchmark

detangle is measured three ways: a hand-written **holdout** of small conflict and
conflict-free configs, a realistic **demo agent** with planted conflicts, and an
in-distribution **mutation suite**. This page has every number, what each one means, and how
far to trust it.

```bash
python -m benchmarks.run_eval                               # mutation suite + holdout, deterministic lane
python -m benchmarks.run_eval --holdout --lanes typesafe    # holdout with a lane (needs its key)
```

The manual `hybrid-eval` GitHub workflow runs the holdout with any lane combination on a
runner, using the repository secrets.

## The three scores

The holdout is 30 small configs that each contain one planted conflict, written in hedged,
colloquial wording without consulting detangle's lexicons, plus 19 configs that look
suspicious but contain no conflict.

| score | what it counts | ideal |
|---|---|---|
| **strict recall** | conflict configs where one finding names an expected rule code *and* points at every file involved: found it, and named the right kind | 30/30 |
| **class-lenient recall** | the same, but any conflict-class code counts: found it on the right files, any label. Duplicate and routing cases get no leniency | 30/30 |
| **holdout FPs** | conflict-free configs that got any conflict-class finding, at any severity | 0/19 |

Lenient is always at least strict; the gap between them is "found it but mislabeled it". For
a linter the false-positive column matters most: people stop reading a noisy linter.

## Holdout results

| configuration | strict | class-lenient | holdout FPs | wall clock |
|---|---|---|---|---|
| deterministic only (default) | 5/30 | 5/30 | **0/19** | ~1 s |
| **TypeSafe lane** | **27/30** | **27/30** | **0/19** | 13–20 s |
| TypeSafe + jury (`haiku`) | 27/30 | 27/30 | 1/19* | ~20 s |
| TypeSafe + screen (`opus`) + jury (`haiku`) | 27/30 | 27/30 | 3/19* | ~3 min |
| screen (`opus`) + jury (`opus`) | 19/30 | 27/30 | 3/19* | ~10 min |
| screen (`opus`) + jury (`haiku`) | 16/30 | 27/30 | 4/19* | ~5 min |
| jury (`haiku`) alone | 8/30 | 10/30 | 1/19* | not recorded |
| jury (`sonnet`) alone | 7/30 | 11/30 | 2/19* | not recorded |

\* Every false positive in every configuration is a jury CONDITIONAL_CONFLICT verdict, the
model's "maybe" bucket, emitted at **advisory** severity: none of them fails CI.

Where the rows come from: the TypeSafe and screen rows were measured 2026-09-17 on GitHub
runners by the `hybrid-eval` workflow (TypeSafe's `jev-latest`; the Anthropic API with
`claude-opus-5` and `claude-haiku-4-5-20251001`). The jury-alone rows were measured
2026-08-31 through `claude -p`, with the since-removed NLI pre-filter choosing the jury's
queue. The August CLI runs of the screen cascades, with a `sonnet` and an `opus` jury, scored
17/30 and 20/30 strict, 27/30 lenient and 4/19 and 2/19 FPs, within a few cases of the API
rows.

What the table says: a pair-level jury alone plateaus near a third of the conflicts whatever
the juror, because the bottleneck is *finding* candidate pairs, not judging them. The screen
fixes candidate formation and reaches 27/30 lenient; TypeSafe reaches the same 27 with the
right labels, no false positives and in seconds. Stacking the screen and jury on TypeSafe
adds no recall, only advisory findings on conflict-free configs. The three conflicts nothing
catches are two skills whose *descriptions* compete for the same trigger and one drifted
duplicate; the deterministic lane has rules for both classes but misses these phrasings.

## How far to trust these numbers

- **The samples are small.** One case is 3.3 points. The plausible range for the true rate:

  | result | 95% interval |
  |---|---|
  | 27/30 | 74% – 97% |
  | 19/30 | 46% – 78% |
  | 16/30 | 36% – 70% |
  | 5/30 | 7% – 34% |
  | 0/19 | 0% – 17% |
  | 3/19 | 6% – 38% |

- **Only the strict gap is statistically real.** TypeSafe's 27 vs the opus cascade's 19
  strict is significant (exact McNemar p ≈ 0.008), but all 8 differing cases are ones the
  cascade found under another label. Lenient recall is identical case for case. 0/19 vs 3/19
  false positives is *not* distinguishable at this size (p ≈ 0.23).
- **The TypeSafe lane was tuned on this holdout.** Its question wording, class list,
  structural overlay, `tau` and co-activation note were each chosen by their holdout score,
  which moved from 23 to 26 to 27 across commits ([experiments.md](experiments.md)). The
  screen and jury prompts were not iterated this way. Treat 27/30 as a development-set score,
  optimistic until a fresh test set exists.
- **"0/19" rests on 21 pairs.** The lane judges only 21 instruction pairs across the benign
  configs (three of them produce no pairs at all), while a realistic config has thousands.
  The holdout says little about false-alarm volume at scale; the demo agent below says more.

## Demo agent

`examples/demo-agent` is a realistic 134-unit config for a fictional TypeScript SaaS, with
14 planted conflicts and 4 benign traps ([EXPECTED.md](../examples/demo-agent/EXPECTED.md)).
Its committed triage baseline records a verdict for every finding.

| lane | planted conflicts found | notes |
|---|---|---|
| deterministic | 9 of 14 (C5–C13) | 0 trap hits |
| TypeSafe (`pairs = "candidates"`) | the 4 semantic ones the deterministic lane cannot phrase: C1, C3, C4, C14 | 0 trap hits; 4 more conflict findings triaged as not conflicts, so 4 of 8 conflict findings were real; the 7 CLAUDE.md/AGENTS.md restatements it reports as duplicates are intentional |
| screen + jury (experimental) | C2 | also 3 real conflicts nobody planted: canary bake minimums, hotfix rollout vs the Friday freeze, the PR size cap vs a completeness rule. The first full-cascade night through the API added 30 new advisory or info-level questions for triage |

Together the three lanes find all 14.

## Scaling: why more TypeSafe queries are not free recall

Pairs grow with the square of the config. At the demo agent's measured rate of about one
wrong conflict finding per 670 judged pairs:

| config size | pairs if every pair is asked | expected false alarms |
|---|---|---|
| 134 units | 8,911 | ~13 |
| 300 units | 44,850 | ~67 |
| 1,000 units | 499,500 | ~750 |

Asking every pair also did not find more planted conflicts on the demo agent (see
[experiments.md](experiments.md)), and asking the same question again returns the same answer
within about ±0.01. Extra budget is better spent on *new kinds* of questions aimed at known
gaps: skill-description overlap, drift between near-duplicates, whole-procedure ordering, and
a "name a scenario where both apply" check on emitted findings.

## Mutation suite (in-distribution)

Nine conflict-injection operators over clean config trees, with equivalent-mutant controls:
108/108 pair-granular detection, 0/24 control false positives. The injections are phrased in
vocabulary the deterministic lane understands, so this measures self-consistency, not
generalization; the holdout is the generalization estimate.

## What would settle "which lane is best"

A fresh, sealed test set nobody has tuned on: 30–60 real public agent configs (CLAUDE.md,
AGENTS.md, Cursor and Copilot rules, skills) spanning small to large, labeled by someone who
has not seen detangle's prompts, with benign look-alikes for every conflict shape.
