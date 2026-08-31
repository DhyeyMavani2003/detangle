# Triage: the baseline and deep scans

The benchmark in [lanes.md](lanes.md) is blunt about the trade: the deterministic core is
free, instant, and catches 17% of the novel-phrasing holdout; the full screen + jury
cascade catches 90% class-lenient and takes strong-model calls to do it. That creates two
problems any linter must solve before a team runs it twice:

1. **A thorough scan does not fit in a PR check.** Whole-config screening with a frontier
   model takes minutes to hours, not seconds.
2. **A thorough scan of a mature config surfaces dozens of findings** — and without
   memory, the next run surfaces the same dozens again. A tool that re-asks answered
   questions gets turned off.

The triage system solves both with one artifact: a checked-in **baseline** file that
records every finding ever seen and every human verdict ever given. Thorough scans run
overnight; a human answers each question exactly once; every later run pre-fills the
answers and reports only what is genuinely new. A config with 40 known findings and 1 new
one surfaces exactly **1** item.

---

## The baseline file

By default **`.detangle-baseline.json`** at the scanned root (override with
`--baseline PATH` or `[detangle.baseline] path`). It is plain JSON, designed to be
**hand-editable and reviewed in PRs** — editing it directly is as supported as the
`detangle baseline` subcommand, and a baseline diff in a PR is a readable record of what
was found and what was decided. Each entry carries:

| Field | Meaning |
|---|---|
| `fingerprint` | Stable content-addressed id, `CODE:hash` — the entry's primary key and what `detangle baseline set` addresses (a unique prefix works). |
| `pair_key` | Code-independent identity for the pair of instructions involved (see below). |
| `code` | The rule code (`DTC01`, `DTP02`, …). |
| `status` | The human verdict: `new` / `open` / `accepted` / `resolved`. |
| `note` | Your justification. Write one — the baseline is the record future readers get. |
| `message`, `severity`, `files`, `quotes` | A snapshot of the finding, so the baseline is reviewable on its own without re-running the scan. For LLM-lane findings, `message`/`quotes` keep the wording from the first sighting — a jury re-verdict phrases itself differently every night, and the checked-in artifact must not churn. |
| `lanes` | Which analysis lanes produced the finding. A cheaper later run (a deterministic-only PR gate after a deep nightly) does **not** mark deep-lane entries missing — it counts them `unchecked`, because that run could never have seen them. |
| `first_seen` | Date the finding first appeared. |
| `missing_since` | Date the finding stopped appearing (stamped once; not set while the finding is present, and never stamped by a run whose lanes couldn't have seen the entry). |

### Identity: what survives what

Every entry carries two ids because two different things must survive:

- **`fingerprint` survives line moves.** It is content-addressed (`CODE:hash`): unit ids
  hash the normalized instruction text plus its file path — never line numbers — so
  reflowing a file, inserting sections above, or editing unrelated paragraphs changes
  nothing. The fingerprint changes only when the instruction text itself (or the file it
  lives in) changes — which is exactly when a human should look again.
- **`pair_key` survives re-classification.** LLM lanes can legitimately re-classify a
  marginal pair between runs — CONTRADICTORY today, CONDITIONAL_CONFLICT tomorrow, which
  is DTC01 vs DTC02 and therefore a different fingerprint. Your verdict was about the
  *pair of instructions*, not about the code a lane happened to choose, so the verdict is
  matched by `pair_key` as well: a re-classified pair keeps its human answer instead of
  resurfacing as "new".

Matching is deliberately conservative: exact fingerprint matches are claimed first
(across the whole run — a sibling-code finding can never steal an entry that
byte-exactly belongs to another finding), pair_key adoption then applies only to what
remains, and an *ambiguous* adoption (several candidates that no code or message
tie-break separates) is refused — the finding surfaces as new rather than inheriting a
verdict that might belong to something else. "Asks again" is the safe failure;
"silently suppressed with someone else's answer" is not.

---

## Statuses: the four answers

An entry's `status` is the answer to one question — *what did a human decide about this
finding?*

| Status | Meaning | In reports | Under `--fail-on-new` |
|---|---|---|---|
| `new` | Nobody has looked yet — the triage queue. | shown | **fails** (at/above `fail_on`) |
| `open` | A human confirmed it is real; the fix is pending. | shown as known; hidden by `--only-new` | passes — known debt does not block builds |
| `accepted` | A human decided this is **not** a conflict. | suppressed entirely | passes |
| `resolved` | A human says it was fixed. | absent — unless it reappears | a reappearance fails |

Two mechanics worth spelling out:

- **Regressions.** If a `resolved` finding reappears, the entry flips back to `new` and
  the report flags it as a **REGRESSION** — the strongest signal the system produces,
  because someone explicitly claimed this was fixed.
- **Disappearance.** When a finding stops appearing, its entry is *not* deleted — it gets
  `missing_since` stamped once and is kept for history, so `resolved` entries can catch
  regressions and `accepted` verdicts survive a finding flickering out and back in. Run
  `detangle baseline prune` when you want the missing entries deleted.

`accepted` is deliberately permanent: once a human says "not a conflict", that answer is
pre-filled on every future run, forever. This is the difference between a baseline and a
snooze button.

One more safety property: an unreadable or corrupt baseline file is **never overwritten**
— the scan degrades to no-baseline behavior with a warning, and `detangle baseline
set`/`prune` refuse to run, because rewriting the file would destroy the verdicts it
still physically contains. Fix or restore the file (it lives in git) and re-run.

---

## The workflow: questions and answers

The intended loop treats every untriaged finding as a question addressed to a human, and
the baseline as the answer sheet:

1. **Overnight**, CI runs the thorough pass:

   ```bash
   detangle scan --deep --baseline --update-baseline
   ```

   Findings the baseline has never seen are recorded with `status: "new"`; findings that
   disappeared get `missing_since` stamped; everything already answered stays answered.

2. **In the morning**, list the questions:

   ```bash
   detangle baseline list --status new
   ```

3. **Answer each one** — by fingerprint or any unique prefix:

   ```bash
   detangle baseline set DTC03:9f3a open     --note "real; fix with the retry cleanup"
   detangle baseline set DTC05:2c81 accepted --note "hotfix carve-out is intentional"
   detangle baseline set DTR01:77ab resolved --note "deduplicated in #142"
   ```

   Or edit `.detangle-baseline.json` directly and commit it — the file is meant to be
   hand-edited, and the PR review of a baseline change *is* the triage record.

4. **The next run pre-fills every prior answer.** With `--only-new`, the report contains
   only what the baseline could not answer: genuinely new findings and regressions.
   Nothing is ever re-asked.

---

## `--deep`: thoroughness-first scanning

The default scan is precision-first and budgeted — the right shape for interactive use
and PR gates. `--deep` flips the priority to thoroughness:

- **every available lane** is enabled (NLI if installed, screen + jury if any backend is
  available — see [lanes.md](lanes.md));
- the screen runs **per-class sweeps** — ten strong-model passes instead of one, each
  hunting a single conflict class, instead of one pass asked to notice everything;
- the **jury cap lifts to 1000** pairs (from the default 200).

`--deep` is designed for scheduled, overnight CI: hours are acceptable, because nobody is
waiting on the result — the output is a refreshed baseline and a short morning triage
queue, not a blocked merge. The persistent form is top-level `deep = true` in TOML; keep
that in a dedicated CI config (selected with `--config`) rather than the file your editor
hooks and PR checks read.

---

## The churn guarantee

A checked-in artifact is only tolerable if it does not churn. detangle guarantees: **two
identical runs over an unchanged tree write byte-identical baselines.** Dates
(`first_seen`, `missing_since`) are stamped only when something appears or disappears —
never re-stamped on every run — so the checked-in file diffs **only when reality
changes**. `git log` on `.detangle-baseline.json` reads as a history of the config's
conflict surface, and an auto-commit step in CI (below) produces empty diffs, not noise,
on quiet nights.

---

## Configuration and CLI

The full reference lives in [configuration.md](configuration.md); the short version:

```toml
# a dedicated CI config, e.g. detangle-ci.toml, selected with --config
deep = true

[detangle.baseline]
path = ".detangle-baseline.json"
update = true
```

| Flag / command | Meaning |
|---|---|
| `--deep` | Thoroughness-first run (every lane, per-class screens, jury cap 1000). |
| `--baseline [PATH]` | Use a baseline; a bare `--baseline` means `.detangle-baseline.json` at the scan root. |
| `--update-baseline` | Write the post-scan state back to the baseline file. |
| `--only-new` | Report only `new` findings and regressions. |
| `--fail-on-new` | Exit non-zero only for `new`/regression findings at or above `fail_on`. |
| `detangle baseline list [--status S]` | List entries; `--status new` is the triage queue. |
| `detangle baseline set FP STATUS [--note ...]` | Answer a question by fingerprint (or prefix). |
| `detangle baseline prune` | Delete entries whose finding has disappeared. |

`--baseline`, `--update-baseline`, `--only-new`, `--fail-on-new`, and `--deep` work on
`detangle diff` as well as `scan`; the `baseline` subcommands take the scan root as an
optional path argument and `--baseline FILE` to override the file location.

---

## CI recipes

### The nightly deep scan

This repo runs the loop against `examples/demo-agent` — the realistic showcase config —
in [`.github/workflows/nightly-deep-scan.yml`](../.github/workflows/nightly-deep-scan.yml).
The generic shape for any repo:

```yaml
name: nightly-deep-scan
on:
  schedule:
    - cron: "0 3 * * *"
  workflow_dispatch:

jobs:
  deep-scan:
    runs-on: ubuntu-latest
    timeout-minutes: 360          # hours are acceptable — that is the point
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install detangle

      - name: Deep scan against the baseline
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
        run: |
          status=0
          detangle scan --deep --baseline --update-baseline \
            --only-new --fail-on-new \
            --format markdown --output deep-scan.md || status=$?
          cat deep-scan.md >> "$GITHUB_STEP_SUMMARY"
          exit "$status"

      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: deep-scan-report
          path: deep-scan.md

      # Optional: commit the refreshed baseline so morning triage starts from it.
      # (On quiet nights the baseline is byte-identical and this step is a no-op.)
      - name: Commit refreshed baseline
        if: always()
        run: |
          git config user.name  "detangle nightly"
          git config user.email "actions@users.noreply.github.com"
          git add .detangle-baseline.json
          if ! git diff --cached --quiet; then
            git commit -m "chore: refresh detangle baseline"
            git push
          fi
```

Notes on the shape:

- `--fail-on-new` makes the job red exactly when there is something for a human to look
  at — new findings or regressions — and green when the night found nothing new, even if
  dozens of `open` findings are still pending. The job's color *is* the triage signal.
- `--only-new` keeps the Markdown summary down to the actual questions.
- If your default branch is protected, replace the commit step with a PR-opening action
  (e.g. `peter-evans/create-pull-request`) so the refreshed baseline arrives as a
  reviewable PR instead of a direct push.
- The jury backend is whatever you have (see [lanes.md](lanes.md)); the snippet shows the
  Anthropic API key variant.

### The PR-time gate

PRs need seconds, not hours — so the gate is deterministic-only:

```yaml
- uses: actions/checkout@v4
- run: pip install detangle
- run: detangle scan --baseline --fail-on-new     # deterministic lane only; seconds
```

The deterministic lane's findings are byte-reproducible, so this gate is stable, free,
and network-less — and because it fails only on `new`/regression entries, the known
backlog triaged as `open` never blocks anyone's merge. The overnight `--deep` job is what
keeps the baseline's coverage deep; the PR gate just holds the line.
