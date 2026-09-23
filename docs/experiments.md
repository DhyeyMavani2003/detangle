# Experiments and design history

This page is the lab notebook: the measurements behind the lanes' current design, including
the ideas that were measured and rejected. Users do not need it; contributors changing a
lane should read it first. Headline numbers and their caveats are in
[benchmark.md](benchmark.md).

Every TypeSafe number below was measured on the 49-tree holdout, which makes that holdout a
development set for the lane (see the caveat in [benchmark.md](benchmark.md)).

## TypeSafe lane: what moved the number

Each step measured live, cumulative (strict / lenient / false positives):

| step | result |
|---|---|
| 4-option relation Choice, precision-first extraction | 11/30 strict: extraction was the ceiling, not judgment |
| + high-recall extraction, switched on with the lane like the screen | 19 / 26 / 0 |
| + 9-option vocabulary, one clash mechanism per option, "not for" clauses | 25 / 26 / 0 |
| + structural overlay mirroring the deterministic router (skill layer vs other → DTP04, overlapping globs → DTP02) | 26 / 26 / 0 |
| + the pair's co-activation class, account and precedence account inside the question | 27 / 27 / 0 (the overlapping-glob pair went from 0.45 to 0.96) |

Measured and **not** adopted: a yes/no question as the gate (margin 0.01 vs 0.15+ for the
Choice mass), structured criteria objects (+1 with the old vocabulary at 3.3× the tokens), an
inverted "can it comply with both" framing (−2), a statement framing (0), a 4-level severity
Score question (narrower plateau, higher benign ceiling), and a "not a narrowing" clause on
the conditional option (−6). Zero false positives held at every `tau` from 0.5 to 0.95 in
every variant, but those are re-judgments of the same 21 benign pairs, not independent
confirmations.

## State size and the two-pass design

Judgment degrades as the shared state grows. Planted demo pairs judged alone vs inside a
95-pair batch: C1 1.00 vs 0.96, C3 0.94 vs 0.38, C4 0.97 vs 0.58, C14 0.90 vs 0.79. Batch size
is therefore a quality knob, not a free cost lever; `pairs_per_call` defaults to 20.

The second pass (`rejudge = true`) re-asks every pair the batched pass puts at or above
`uncertain_low` alone. On the demo agent it re-asked 116 pairs in about 50 s: 55 of the 109
band pairs cleared, so the jury receives half the pairs; the four planted pairs the lane owns
stayed emitted; one batched finding a human had triaged as not a conflict dropped to the
band. The cost: one soft goal-tension pair a human had marked `open` cleared. Letting the solo
verdict *replace* the batched one was measured and rejected: it promoted ten borderline
pairs, three of which a human had already rejected, for one genuine conflict the jury finds
from the band anyway.

## Pair set: all pairs vs candidates

On the holdout, asking every co-activatable pair instead of the deterministic lane's
candidate pairs added about 4 cases, because holdout trees are tiny and a pair the
candidate generator misses is a missed case.

On the demo agent the picture reversed. An all-pairs sweep with an early lane version (a
4-option vocabulary, about 41 pairs per call, one pass, no co-activation account) judged
7,956 pairs in 194 calls with 6.2M input and 1.2M output tokens:

| `tau` | findings | planted found | trap hits | other |
|---|---|---|---|---|
| 0.5 | 17 | 3 (C1, C3, C4) | 2 | 9 |
| 0.6 | 7 | 3 | 1 | 1 |
| 0.7 | 2 | 2 (C1, C3) | 0 | 0 |
| 0.8+ | 0 | 0 | 0 | 0 |

Candidates mode with the shipped lane finds C1, C3, C4 and C14 at `tau` 0.7 with no trap hits.
The two runs used different lane versions, so the comparison is a warning rather than a
controlled result, and all-pairs mode has not been re-measured with the shipped lane.

## Where C2 actually lives

The demo agent's C2 was long described as an order carried by list position across three
lines, invisible to any pair question. That is wrong. The jury catches it on one sentence
pair: CLAUDE.md's "it lints first, then typechecks, then runs the unit suite" vs the
pre-commit skill's "Run `pnpm lint` last". The all-pairs sweep asked TypeSafe that exact pair
and scored it 0.17. So C2 is a judgment miss by TypeSafe, not a structural blind spot; a
0.07 figure quoted earlier belonged to a different pair from the same skill.

## Extractor audit (measured, not landed)

Per-unit TypeSafe questions make a sharp audit of the deterministic extractor: on the demo
agent it drops 40 of 134 directives and misreads modality on 7 of 75 strict units, for
example `can` inside a purpose clause read as a permission, or a trailing "never the other
way around" inverting an imperative. Feeding those answers back into the deterministic
detectors measured +1/30 on the holdout (frames, not modality, are the binding constraint),
and five adversarial review rounds could not make any of the matching lexicon fixes
precise: each traded a corrected modality for a new false positive, a severity escalation or
a lost finding on realistic prose. None of them shipped.

## The NLI lane (removed)

An NLI cross-encoder (`cross-encoder/nli-deberta-v3-small`) was the first semantic lane, used
as a recall filter in front of the jury. Measured on declarativized instruction pairs, true
contradictions scored about 1.00, but so did pairs of merely *different* prescriptions ("must
run tests" vs "must write documentation" scored 0.99 under every normalization template);
paraphrases scored about 0.00. So the lane could only ever auto-clear, never flag, and on the
holdout it changed nothing (deterministic 5/30 with or without it). It never emitted a
finding, had no dedicated tests, and its extra pulled in about 4 GB of torch. The TypeSafe
lane's calibrated band clears pairs better, so the NLI lane was removed.

## Ideas worth measuring next

Each targets a known gap, and each needs a positive case and a close-but-benign control in
`tests/test_detectors.py` and a fresh test set before it ships:

- a **skill-description overlap** question for two skills that compete for the same trigger
  (the two routing misses on the holdout);
- a **drift** question on pairs already judged related (the drifted-duplicate miss);
- a **whole-procedure ordering** question over step lists;
- a **"name a scenario where both apply"** check on emitted findings only, aimed at the demo
  agent's precision gap;
- cheap per-unit topic tags to block pairs, so large configs stay below quadratic cost.
