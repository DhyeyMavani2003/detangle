# detangle

**A linter for AI agent instructions.** detangle reads your `CLAUDE.md`, `AGENTS.md`, skills,
Cursor rules and Copilot instructions, and reports instructions that contradict each other,
duplicate each other, or get silently overridden. Each finding shows both sides with file and
line, explains when the two load together, and suggests a fix.

```
╭─ DTC03 quantitative-conflict  [error] ──────────────────────────────────────╮
│ Numeric constraints disagree: 'at most 3 times' and 'exactly 5 times'       │
│ cannot both hold (ranges do not intersect).                                 │
│                                                                             │
│   CLAUDE.md:3                       "Retry flaky tests at most 3 times."    │
│   .claude/skills/fix-ci/SKILL.md:8  "Retry flaky tests exactly 5 times      │
│                                      before marking the build failed."      │
│                                                                             │
│   co-activation: one loads at launch; the other is description-triggered    │
│                  (under claude-code)                                        │
│   precedence:    cross-mechanism pair (memory vs skill): no ecosystem       │
│                  documents which one wins                                   │
│   fix:           Pick one limit and delete the other, or scope each to the  │
│                  situation it belongs to.                                   │
╰─────────────────────────────────────────────────────────────────────────────╯
```

Agents do not resolve these conflicts reliably: Anthropic's docs say that when two rules
contradict, *"Claude may pick one arbitrarily"*. The cheap place to catch a conflict is before
the agent runs. [More background](docs/background.md).

## Quick start

detangle is not on PyPI yet; install it from GitHub:

```bash
pip install git+https://github.com/DhyeyMavani2003/detangle
cd your-project
detangle scan
```

The default scan needs no API key, makes no network calls and gives the same result every
time, so it is safe as a CI gate. Findings come in four severities: `error`, `warning`,
`advisory` and `info`. By default only an `error` makes the run exit non-zero; set `fail_on`
to gate on warnings too.

```bash
detangle explain DTC03              # what a rule means, with a link to its full docs
detangle diff --base origin/main    # only findings in config files your branch changed
detangle rules                      # every rule
```

It understands how each tool loads its files: Claude Code (`CLAUDE.md`, rules, skills,
subagents, commands), the `AGENTS.md` family (Codex, Zed and others), Cursor rules and GitHub
Copilot instructions. Instructions that can never be in context together, such as rules
for disjoint paths, are not reported as conflicts. A clash between files that different
tools read is still reported, because the agent then behaves differently depending on the
tool. [How each tool loads files](docs/ecosystems.md).

## Thorough pass: TypeSafe (optional)

The default scan catches conflicts with a clear signal: numbers, formats, permit vs forbid,
duplicates, overrides, missing files. Conflicts phrased in looser English, like "use pnpm"
in one file and "install with npm" in another, need a model that judges meaning.
[TypeSafe](https://typesafe.ai) is a separate hosted service that answers typed questions
with probabilities; with an account key, `--typesafe` asks it to classify every pair of
instructions that can load together:

```bash
export TYPESAFE_API_KEY=...
detangle scan --typesafe
```

This sends your instruction text to TypeSafe's API. Answers are cached, so re-scanning an
unchanged config makes no calls. Without the key the scan still runs and says the TypeSafe
pass was skipped. Two experimental passes run on any LLM instead, including the Claude Code
CLI or a local model: a *screen* that reads the whole config and nominates suspicious pairs,
and a *jury* that judges them. All the passes ("lanes") are described in
[docs/lanes.md](docs/lanes.md).

## How well it works

On a hand-written benchmark of 30 conflicts and 19 conflict-free look-alikes:

| | found, right kind | found, any kind | false alarms | time |
|---|---|---|---|---|
| default scan | 5/30 | 5/30 | 0/19 | ~1 s |
| + TypeSafe | **27/30** | **27/30** | **0/19** | ~15 s |
| experimental screen + jury (Claude Opus) | 19/30 | 27/30 | 3/19* | ~10 min |

\* All advisory findings, which never fail CI.

"Right kind" means the finding also names the correct conflict class. Read the TypeSafe row
with two caveats: the lane was tuned on this same benchmark, and with 30 cases the plausible
range for 27/30 is 74–97%. The default scan's 5/30 is low because the benchmark was written
in wording its rules do not know; on the realistic demo config in `examples/demo-agent` it
finds 9 of 14 planted conflicts. [Every configuration, the statistics, and the demo
config](docs/benchmark.md).

## What it checks

22 rules in five classes ([all rules with examples](docs/taxonomy.md)):

| Class | Examples |
|---|---|
| **Conflicts** (DTC) | "always X" vs "never X" · "at most 3 retries" vs "exactly 5" · "respond with JSON only" vs "always respond in markdown" · permit vs forbid · "be concise" vs "explain in detail" |
| **Precedence** (DTP) | a rule that can never take effect · overlapping scopes with no declared winner · a skill contradicting `CLAUDE.md` · text cut off by a size limit · tools reading different files |
| **Redundancy** (DTR) | duplicates · paraphrases drifting apart · a term defined two ways · references to files that do not exist |
| **Routing** (DTS) | skills competing for the same trigger · a description its body does not deliver · name shadowing |
| **Security** (DTX) | invisible Unicode and hidden HTML-comment directives · a lower-priority file granting what a higher-priority one forbids |

## Use it in CI

```yaml
- uses: actions/checkout@v4
- uses: DhyeyMavani2003/detangle@main
  with:
    args: scan --format sarif --output detangle.sarif
- uses: github/codeql-action/upload-sarif@v3
  if: always()     # detangle exits 1 on an error finding; upload the report anyway
  with:
    sarif_file: detangle.sarif
```

Code-scanning upload needs `permissions: security-events: write` on the job, plus
`contents: read` and `actions: read` in a private repository.

On a repo that already has findings, record them once, mark them as the known backlog, and
gate only on findings that appear later:

```bash
detangle scan --baseline --update-baseline    # record today's findings
detangle baseline adopt                       # mark them all "open"; commit .detangle-baseline.json
detangle scan --baseline --fail-on-new        # in CI: fails only on findings nobody has seen
```

Answering findings one by one, and nightly thorough scans, are in
[docs/triage.md](docs/triage.md).

## Configure

An optional `.detangle.toml` sets the failing severity and turns rules off or changes their
severity ([reference](docs/configuration.md)). To silence one finding where it occurs, give
a reason:

```markdown
<!-- detangle-ignore DTC05: hotfix exception is intentional until Q3 -->
- Feel free to push directly to main for hotfixes.
```

## Docs

- [lanes.md](docs/lanes.md): the deterministic, TypeSafe and experimental LLM lanes
- [benchmark.md](docs/benchmark.md): every measured number and how far to trust it
- [taxonomy.md](docs/taxonomy.md): every rule, with examples and fixes
- [ecosystems.md](docs/ecosystems.md): how each tool loads and prioritizes its files
- [configuration.md](docs/configuration.md): config keys, CLI flags, environment variables
- [triage.md](docs/triage.md): baselines, nightly scans, CI recipes
- [experiments.md](docs/experiments.md): design history and ideas to measure next
- [CONTRIBUTING.md](CONTRIBUTING.md): development setup and architecture

## License

MIT.
