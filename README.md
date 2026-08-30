# detangle

**Merge-conflict detection and CI for English-as-code.**

Your agent's configuration — `CLAUDE.md`, `AGENTS.md`, skills, rules, subagents — is a
program written in English, edited by many hands, executed by a model. Code gets merge-conflict
detection, linters, type checkers, and CI. Your agent's English gets none of that.

`detangle` statically analyzes an agent's full natural-language configuration and reports
**conflicting, contradictory, redundant, shadowed, and precedence-ambiguous instructions** —
each with evidence spans, a co-activation account, and a precedence account.

```
┌ DTC03 quantitative-conflict  [error] ─────────────────────────────────────────┐
│ Numeric constraints disagree: 'at most 3 times' and 'exactly 5 times'         │
│ cannot both hold (ranges do not intersect).                                   │
│                                                                               │
│   CLAUDE.md:6   "Retry flaky tests at most 3 times."                          │
│   AGENTS.md:4   "Retry flaky tests exactly 5 times."                          │
│                                                                               │
│   co-activation: both load at launch under copilot                            │
│   precedence:    CLAUDE.md and AGENTS.md belong to different config surfaces; │
│                  any tool reading both provides them side by side with no     │
│                  documented precedence                                        │
│   fix:           Pick one limit and delete the other, or scope each to the    │
│                  situation it belongs to.                                     │
└───────────────────────────────────────────────────────────────────────────────┘
```

## Why

The vendors already admit the problem:

- Anthropic's docs: *"if two rules contradict each other, Claude may pick one arbitrarily."*
- OpenAI's GPT-5 guide: contradictory instructions cause the model to *"expend reasoning
  tokens searching for a way to reconcile the contradictions."*
- In July 2026, Anthropic found conflicting directives in Claude Code's own configuration
  ("leave documentation as appropriate" vs "DO NOT add comments") and removed over 80% of its
  system prompt with no measured loss.

And the research is unambiguous: models cannot resolve instruction conflicts at runtime
(best open model ~48% on IHEval; which rule wins is position- and model-dependent), config
files accrete conflicts structurally (+4.9 instructions per commit touching them), and
91/100 real AGENTS.md/CLAUDE.md files carry at least one config smell. The place to catch a
conflict is **lint time**, not inference time.

## What it checks

21 rules across five classes (see [docs/taxonomy.md](docs/taxonomy.md)):

| Class | Codes | Examples |
|---|---|---|
| **C — Conflicts** | DTC01–08 | "always X" vs "never X" · "≤3 retries" vs "exactly 5" · "JSON only" vs "markdown" · permit vs forbid · "be concise" vs "explain in detail" |
| **P — Precedence & reachability** | DTP01–06 | shadowed rules · overlapping scopes with no declared winner · cross-layer collisions (skill vs CLAUDE.md) · instructions silently dropped by size budgets (Codex's 32 KiB halt, skill-listing truncation) · different tools reading different files |
| **R — Redundancy & drift** | DTR01–05 | duplicates · paraphrases drifting apart · a term defined two ways · restating what your linter already enforces · references to files/commands that don't exist |
| **S — Selection & routing** | DTS01–03 | skills competing for the same trigger words · description ≠ body · name shadowing |
| **X — Security** | DTX01–02 | invisible Unicode & hidden HTML-comment directives · a lower tier granting what a higher tier forbids |

What makes the analysis different from a format linter:

- **Co-activation aware.** Two instructions that can never be in context together cannot
  conflict. detangle models each ecosystem's activation semantics — launch sets, glob-scoped
  rules, description-triggered skills, isolated subagent contexts — and prunes impossible
  pairs exactly, before any semantic judgment.
- **Precedence aware.** "Resolved by a declared hierarchy" is not a conflict; "no declared
  order" is. detangle encodes each ecosystem's documented precedence (including the polarity
  flips: Claude Code skills are personal > project, but subagents are project > user) and
  phrases every finding accordingly.
- **Witness scenarios.** For conditional conflicts, the finding includes the boundary
  condition under which both instructions apply and cannot be jointly satisfied.
- **Deterministic core.** The default mode uses zero LLM calls, zero network, and is fully
  reproducible — safe for CI and air-gapped repos. Optional NLI and LLM-jury lanes add
  semantic depth (see [docs/lanes.md](docs/lanes.md)).

## Install

```bash
pip install detangle            # deterministic core (no ML dependencies)
pip install 'detangle[nli]'     # + local NLI cross-encoder lane
pip install 'detangle[jury]'    # + LLM jury lane (needs ANTHROPIC_API_KEY)
```

Or from this repo: `pip install git+https://github.com/DhyeyMavani2003/detangle`.

## Use

```bash
detangle scan                    # scan the current repo, pretty output
detangle scan --format sarif -o detangle.sarif   # GitHub code-scanning
detangle diff --base origin/main # only findings introduced by your changes
detangle explain DTP02           # what a rule means and how to fix it
detangle rules                   # list all rules
```

Exit code is non-zero when findings at or above `fail_on` severity exist (default: `error`),
so `detangle scan` drops straight into CI.

### GitHub Action

```yaml
- uses: DhyeyMavani2003/detangle@main
  with:
    args: scan --format sarif --output detangle.sarif
- uses: github/codeql-action/upload-sarif@v3
  with:
    sarif_file: detangle.sarif
```

### Configuration

`.detangle.toml` at the repo root ([full reference](docs/configuration.md)):

```toml
[detangle]
ecosystems = ["claude-code", "agents-md", "cursor", "copilot"]
fail_on = "error"

[detangle.rules]
DTR04 = false        # disable a rule
DTC08 = "info"       # change a severity
```

Suppress a single finding where it occurs, with a required justification:

```markdown
<!-- detangle-ignore DTC05: hotfix exception is intentional until Q3 -->
- Feel free to push directly to main for hotfixes.
```

## What it understands

| Surface | Semantics modeled |
|---|---|
| **Claude Code** | CLAUDE.md hierarchy (concatenation, `@imports` ≤4 hops, subdir on-demand loading), `.claude/rules` (`paths:` globs), skills (description triggers, 1,536-char listing cap, name shadowing), subagents (isolated contexts, project>user), commands |
| **AGENTS.md family** | root + nested files, Codex positional override reading, the 32 KiB discovery halt, per-tool reader divergence (Zed reads *one* file; Claude Code reads none) |
| **Cursor** | `.cursor/rules/*.mdc` four rule types (`alwaysApply`/globs/description/manual), nested subtree scoping, legacy `.cursorrules`, `.md`-in-rules-dir dead files |
| **Copilot** | `.github/copilot-instructions.md`, `instructions/*.instructions.md` with `applyTo`, everything-co-loads union semantics |

Full details in [docs/ecosystems.md](docs/ecosystems.md).

## Benchmark

`benchmarks/` contains the seeded-conflict evaluation harness: clean config trees, nine
conflict-injection operators (deontic flip, parameter clash, scope-overlap clash, conditional
contradiction, terminology drift, cross-layer clash, duplication, format clash, trigger
overlap), and equivalent-mutant controls to measure false-positive rates:

```bash
python -m benchmarks.run_eval
```

Detection and false-positive rates per operator are how detangle's thresholds get set —
measured, not guessed.

## Roadmap

- **Formal lane**: clingo/ASP + Z3 encodings for the formalizable subset (quantitative limits,
  permissions with scopes, ordering) with unsat-core witnesses — "proof modulo translation."
- **Jury ensembles**: 3 disjoint-family jurors with escalation, per the jury protocol in
  [docs/lanes.md](docs/lanes.md).
- **Precedence manifest**: declare intended resolution order (`overrides:` front-matter);
  detangle checks against it instead of flagging ambiguity.
- **PR semantic diff**: "this change makes rule R newly shadowed; widens what the agent may do."

## License

MIT.
