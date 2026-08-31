"""Hand-authored HOLDOUT benchmark: novel phrasings, not detangle's vocabulary.

The mutation suite in :mod:`benchmarks.mutators` is *in-distribution by
construction*: operators select and phrase injections through detangle's own
parser and lexicons, so its detection rate measures self-consistency, not
generalization. This module is the out-of-distribution counterpart. Every case
below was written the way a real config author writes — hedges ("it's best if
you", "we'd rather you didn't"), colloquial verbs ("stick to", "steer clear
of", "hold off on"), post-positive and colloquial bounds ("cap X at N", "keep
it under", "no more than", "past 800"), passive voice, conflicts split across
files and layers — WITHOUT consulting detangle's lexicons or tuning the text
until the detector fires. If detangle misses a case, the miss is the datum.

Two sets:

- ``CONFLICT_CASES`` — at least 24 genuine conflicts covering every conflict
  class detangle claims (DTC01/02/03/04/05/08, DTP02/04, DTR01/02/03, DTS01).
  Each is ``{id, tree, expected_codes, involved_files, description}`` where
  ``tree`` is a ``{relpath: content}`` repo, ``expected_codes`` lists the
  taxonomy codes that count as a detection (primary code first), and
  ``involved_files`` names the files carrying the conflicting text.
- ``BENIGN_CASES`` — at least 16 benign-but-tricky trees (negation-dense but
  compatible, exception carve-outs, numeric bands, paraphrase + scope splits)
  where NO conflict-class code (``HOLDOUT_FP_CODES``) may fire. They carry no
  ``expected_codes``: any conflict-class finding is a false positive.
"""

from __future__ import annotations

#: Conflict-class codes that count as a false positive on a benign tree.
#: DTP03 is deliberately absent: per the taxonomy, DTP03 flags INTENTIONAL
#: exception carve-outs as fragile at advisory severity — firing it on a
#: legitimate carve-out is the designed behavior, not a false positive.
HOLDOUT_FP_CODES = frozenset(
    {
        "DTC01",
        "DTC02",
        "DTC03",
        "DTC04",
        "DTC05",
        "DTP01",
        "DTP02",
        "DTP04",
        "DTX02",
    }
)

#: Every conflict class detangle claims; the holdout must cover each.
HOLDOUT_CLAIMED_CODES = (
    "DTC01",
    "DTC02",
    "DTC03",
    "DTC04",
    "DTC05",
    "DTC08",
    "DTP02",
    "DTP04",
    "DTR01",
    "DTR02",
    "DTR03",
    "DTS01",
)

Case = dict[str, object]


def _case(
    case_id: str,
    tree: dict[str, str],
    expected_codes: list[str],
    involved_files: list[str],
    description: str,
) -> Case:
    return {
        "id": case_id,
        "tree": tree,
        "expected_codes": list(expected_codes),
        "involved_files": list(involved_files),
        "description": description,
    }


def _benign(case_id: str, tree: dict[str, str], description: str) -> Case:
    return {"id": case_id, "tree": tree, "description": description}


# ---------------------------------------------------------------------------
# Conflict cases (novel phrasings; the primary expected code comes first)
# ---------------------------------------------------------------------------

CONFLICT_CASES: list[Case] = [
    # ---- DTC01: direct contradictions --------------------------------------
    _case(
        "dtc01-package-manager",
        {
            "CLAUDE.md": """\
# Web client — working notes

## Tooling
Stick to pnpm when installing or updating dependencies; the lockfile in this
repo is pnpm's and nothing else should touch it.
""",
            "AGENTS.md": """\
# Agent guide

Use npm for dependency changes so that package-lock.json stays in sync with
what CI installs.
""",
        },
        ["DTC01", "DTC05", "DTP04"],
        ["CLAUDE.md", "AGENTS.md"],
        "pnpm-only vs npm-only dependency workflow, split across memory files",
    ),
    _case(
        "dtc01-code-comments",
        {
            "CLAUDE.md": """\
# Style

We'd rather you didn't add comments to the code — names and structure should
carry the meaning on their own.
""",
            ".claude/rules/review.md": """\
Every non-obvious block gets a brief comment explaining why it exists, not
what it does.
""",
        },
        ["DTC01", "DTC05", "DTP04"],
        ["CLAUDE.md", ".claude/rules/review.md"],
        "no-comments preference vs comment-the-why requirement, hedged phrasing",
    ),
    _case(
        "dtc01-force-push",
        {
            "AGENTS.md": """\
# Repo guide

## Git
Please don't ever force-push, even on branches only you touch.

## Tips carried over from the old guide
Feel free to force-push your own feature branches to keep the history
readable.
""",
        },
        ["DTC01", "DTC05"],
        ["AGENTS.md"],
        "force-push forbidden vs invited in the same file after a doc merge",
    ),
    # ---- DTC02: conditional conflicts --------------------------------------
    _case(
        "dtc02-release-freeze",
        {
            "CLAUDE.md": """\
# Release process

If you are working on the release branch, the version number gets bumped as
part of every change you land.

While a code freeze is announced, we'd rather you didn't touch the version
number at all.
""",
        },
        ["DTC02", "DTC01"],
        ["CLAUDE.md"],
        "version bumps obliged on release branch vs discouraged during freezes",
    ),
    _case(
        "dtc02-incident-retry",
        {
            "CLAUDE.md": """\
# CI habits

When a deploy job fails on CI, retry it once before digging into the logs.
""",
            ".claude/rules/incidents.md": """\
During a production incident, hold off on retrying deploy jobs — page the
on-call engineer first.
""",
        },
        ["DTC02", "DTC01", "DTC05"],
        ["CLAUDE.md", ".claude/rules/incidents.md"],
        "retry-once-on-failure vs never-retry-during-incidents, split files",
    ),
    # ---- DTC03: quantitative conflicts -------------------------------------
    _case(
        "dtc03-pr-description-words",
        {
            "CLAUDE.md": """\
# Review culture

Reviewers rely on context: put at least 300 words of background into every
pull request description.
""",
            "AGENTS.md": """\
# Agent guide

Keep pull request descriptions tight — no more than 150 words.
""",
        },
        ["DTC03"],
        ["CLAUDE.md", "AGENTS.md"],
        "PR descriptions must be >=300 words in one file and <=150 in another",
    ),
    _case(
        "dtc03-line-length",
        {
            "CLAUDE.md": """\
# Formatting

Line length is capped at 88 characters; Black enforces that locally.
""",
            "AGENTS.md": """\
# Working notes

Wrap lines once they run past 120 characters.
""",
        },
        ["DTC03"],
        ["CLAUDE.md", "AGENTS.md"],
        "88-character cap vs a 120-character wrap point for the same knob",
    ),
    _case(
        "dtc03-test-timeout",
        {
            "CLAUDE.md": """\
# Testing

Give integration tests up to five minutes before treating them as hung.
""",
            "services/api/AGENTS.md": """\
# Checkout API

Kill any test that runs longer than 90 seconds and mark it failed.
""",
            "services/api/app.py": '"""Checkout API entry point."""\n',
        },
        ["DTC03"],
        ["CLAUDE.md", "services/api/AGENTS.md"],
        "five-minute test allowance vs a 90-second kill rule, cross units",
    ),
    # ---- DTC04: format conflicts -------------------------------------------
    _case(
        "dtc04-prose-vs-bullets",
        {
            "CLAUDE.md": """\
# Answer style

Answers should read as plain prose — skip the bullet points and headers.
""",
            "AGENTS.md": """\
# Agent guide

Structure every reply as a bulleted list so it can be pasted straight into
Slack.
""",
        },
        ["DTC04", "DTC01"],
        ["CLAUDE.md", "AGENTS.md"],
        "prose-only answers vs bulleted-list answers",
    ),
    _case(
        "dtc04-json-vs-markdown-skill",
        {
            "CLAUDE.md": """\
# Billing platform

Summaries are returned as a bare JSON object — no prose before or after it.
""",
            ".claude/skills/summaries/SKILL.md": """\
---
name: summaries
description: Use when the user asks for a summary of recent changes or a digest of merged work.
---
# Summaries

Deliver the summary as a short Markdown report with one heading per section.
""",
        },
        ["DTC04", "DTP04"],
        ["CLAUDE.md", ".claude/skills/summaries/SKILL.md"],
        "bare-JSON summaries in memory vs Markdown-report summaries in a skill",
    ),
    # ---- DTC05: modality conflicts (permit vs forbid/oblige) ---------------
    _case(
        "dtc05-amend-commits",
        {
            "CLAUDE.md": """\
# Git

You're welcome to amend your last commit while it's still unpushed.
""",
            "AGENTS.md": """\
# House rules

Treat commits as immutable once made; amending is off the table here.
""",
        },
        ["DTC05", "DTC01"],
        ["CLAUDE.md", "AGENTS.md"],
        "amending permitted vs commits-are-immutable",
    ),
    _case(
        "dtc05-dev-dependencies",
        {
            "CLAUDE.md": """\
# Dependencies

It's fine to add dev-only dependencies without checking in first.
""",
            ".claude/rules/deps.md": """\
Every new dependency, dev or otherwise, needs sign-off from a maintainer
before it lands.
""",
        },
        ["DTC05", "DTC01"],
        ["CLAUDE.md", ".claude/rules/deps.md"],
        "dev deps permitted freely vs sign-off required for every dependency",
    ),
    # ---- DTC08: pragmatic tension ------------------------------------------
    _case(
        "dtc08-concise-vs-thorough",
        {
            "CLAUDE.md": """\
# Communication

Keep answers short — a sentence or two is usually plenty.
""",
            "AGENTS.md": """\
# Agent guide

Walk through your reasoning step by step in every reply so a reviewer can
audit the chain.
""",
        },
        ["DTC08", "DTC01", "DTC04"],
        ["CLAUDE.md", "AGENTS.md"],
        "be-brief vs show-all-reasoning: jointly degrading soft conflict",
    ),
    _case(
        "dtc08-ship-vs-refactor",
        {
            "CLAUDE.md": """\
# Engineering values

## Velocity
Bias toward shipping: make the smallest change that solves the problem.

## Craft
Leave things better than you found them — take the time to refactor the code
around any file you touch.
""",
        },
        ["DTC08"],
        ["CLAUDE.md"],
        "smallest-change-possible vs always-refactor-nearby, same file",
    ),
    # ---- DTP02: precedence ambiguity (partial scope overlap) ---------------
    _case(
        "dtp02-annotations-tests",
        {
            ".claude/rules/python.md": """\
---
paths: "src/**"
---
Public functions carry type annotations; that's how we read the API surface.
""",
            ".claude/rules/tests.md": """\
---
paths: "**/test_*.py"
---
Skip type annotations in tests — they add noise without catching much.
""",
            "src/app.py": '"""App entry point."""\n',
            "src/test_app.py": '"""App tests."""\n',
        },
        ["DTP02", "DTC02", "DTC01"],
        [".claude/rules/python.md", ".claude/rules/tests.md"],
        "annotations required under src/** vs skipped for **/test_*.py — "
        "the intersection (src/test_app.py) has no declared winner",
    ),
    _case(
        "dtp02-styling",
        {
            ".cursor/rules/components.mdc": """\
---
globs: "src/components/**"
---
Style components with Tailwind utility classes.
""",
            ".cursor/rules/tsx.mdc": """\
---
globs: "**/*.tsx"
---
Component styling lives in CSS modules; steer clear of utility-class
frameworks.
""",
            "src/components/Button.tsx": "export const Button = () => null;\n",
        },
        ["DTP02", "DTC02", "DTC01"],
        [".cursor/rules/components.mdc", ".cursor/rules/tsx.mdc"],
        "Tailwind under src/components/** vs CSS-modules under **/*.tsx",
    ),
    # ---- DTP04: cross-layer conflicts --------------------------------------
    _case(
        "dtp04-migrations-skill",
        {
            "CLAUDE.md": """\
# Database

Always ask before running database migrations, even in dev.
""",
            ".claude/skills/migrate/SKILL.md": """\
---
name: migrate
description: Use when the user asks to create or run a database migration.
---
# Migrations

Apply pending migrations right away; waiting for confirmation just slows the
loop down.
""",
        },
        ["DTP04", "DTC01", "DTC05"],
        ["CLAUDE.md", ".claude/skills/migrate/SKILL.md"],
        "memory says ask-first, the migration skill says apply immediately",
    ),
    _case(
        "dtp04-generated-files",
        {
            "CLAUDE.md": """\
# Version control

Generated files stay out of version control — never commit them.
""",
            ".claude/skills/snapshots/SKILL.md": """\
---
name: snapshots
description: Use when snapshot tests fail and the stored snapshots need regenerating.
---
# Snapshot refresh

Regenerate the snapshots and commit the updated files together with your
change.
""",
        },
        ["DTP04", "DTC01"],
        ["CLAUDE.md", ".claude/skills/snapshots/SKILL.md"],
        "never-commit-generated-files vs a skill that commits regenerated ones",
    ),
    # ---- DTR01: duplicates --------------------------------------------------
    _case(
        "dtr01-cross-file-duplicate",
        {
            "CLAUDE.md": """\
# Project guide

- Run the linter before every commit.
- Prefer small, focused pull requests.
""",
            "AGENTS.md": """\
# Agent guide

- Run the linter before every commit.
""",
        },
        ["DTR01", "DTR02"],
        ["CLAUDE.md", "AGENTS.md"],
        "identical lint rule stated verbatim in two always-on files",
    ),
    _case(
        "dtr01-same-file-duplicate",
        {
            "CLAUDE.md": """\
# Project guide

## Workflow
- Update the changelog with every user-facing change.

## Pre-merge checklist
- Update the changelog with every user-facing change.
""",
        },
        ["DTR01"],
        ["CLAUDE.md"],
        "the same bullet copy-pasted under two headings of one file",
    ),
    # ---- DTR02: near-duplicate drift ---------------------------------------
    _case(
        "dtr02-drifted-test-command",
        {
            "CLAUDE.md": """\
# Project guide

- Run `make test` before pushing.
""",
            "AGENTS.md": """\
# Agent guide

- Before you push, run make test and make lint.
""",
        },
        ["DTR02", "DTR01"],
        ["CLAUDE.md", "AGENTS.md"],
        "copied rule that grew an extra step in one place (make lint)",
    ),
    _case(
        "dtr02-changelog-paraphrase",
        {
            "CLAUDE.md": """\
# Project guide

- Keep the changelog up to date with every user-facing change.
""",
            ".claude/rules/hygiene.md": """\
- Update CHANGELOG.md whenever behaviour changes in a way users can see.
""",
        },
        ["DTR02", "DTR01"],
        ["CLAUDE.md", ".claude/rules/hygiene.md"],
        "same changelog rule paraphrased in two files, drifting on details",
    ),
    # ---- DTR03: terminology inconsistency ----------------------------------
    _case(
        "dtr03-staging",
        {
            "CLAUDE.md": """\
# Environments

"Staging" refers to the shared pre-production cluster behind
staging.internal.
""",
            "AGENTS.md": """\
# Agent guide

"Staging" means whatever docker compose brings up on your machine.
""",
        },
        ["DTR03"],
        ["CLAUDE.md", "AGENTS.md"],
        "'staging' defined as a shared cluster in one file, local compose in another",
    ),
    _case(
        "dtr03-fast-tests",
        {
            "CLAUDE.md": """\
# Testing

- "Fast tests" means anything that finishes in under a second.
""",
            "services/api/AGENTS.md": """\
# Checkout API

- "Fast tests" means the unit suite without the browser tests.
""",
            "services/api/app.py": '"""Checkout API entry point."""\n',
        },
        ["DTR03"],
        ["CLAUDE.md", "services/api/AGENTS.md"],
        "'fast tests' defined by duration in one file and by suite in another",
    ),
    # ---- DTS01: trigger overlap --------------------------------------------
    _case(
        "dts01-release-skills",
        {
            ".claude/skills/release-notes/SKILL.md": """\
---
name: release-notes
description: Use when the user wants release notes or a rundown of what shipped in a release.
---
# Release notes

Collect the merged pull requests since the last tag and write the notes.
""",
            ".claude/skills/changelog/SKILL.md": """\
---
name: changelog
description: Use when asked to summarize shipped changes for a release announcement or changelog entry.
---
# Changelog

Group the shipped changes into Added, Changed, and Fixed sections.
""",
        },
        ["DTS01"],
        [".claude/skills/release-notes/SKILL.md", ".claude/skills/changelog/SKILL.md"],
        "two skills competing for the summarize-what-shipped intent",
    ),
    _case(
        "dts01-rollback-skills",
        {
            ".claude/skills/rollback/SKILL.md": """\
---
name: rollback
description: Use when a deploy has gone bad and needs rolling back or the release pipeline breaks.
---
# Rollback

Identify the last good release and revert to it.
""",
            ".claude/skills/deploy-doctor/SKILL.md": """\
---
name: deploy-doctor
description: Use when the user reports a failed deploy or asks to roll back a bad release.
---
# Deploy doctor

Inspect the failed deploy, then either fix forward or roll back.
""",
        },
        ["DTS01"],
        [".claude/skills/rollback/SKILL.md", ".claude/skills/deploy-doctor/SKILL.md"],
        "two skills both claiming the failed-deploy / rollback intent",
    ),
    # ---- procedural / skill-orchestration conflicts (the main-file-vs-skill
    # and step-ordering scenarios) --------------------------------------------
    _case(
        "proc-lint-test-order",
        {
            "CLAUDE.md": (
                "# Workflow\n\n"
                "Run the linter first, then the test suite; commit only after both pass.\n"
            ),
            ".claude/skills/pre-commit/SKILL.md": (
                "---\n"
                "name: pre-commit\n"
                "description: Use before committing changes to verify the working tree.\n"
                "---\n"
                "# Pre-commit checks\n\n"
                "Start with the test suite so failures surface early, and save linting "
                "for the very end once tests are green.\n"
            ),
        },
        ["DTC02", "DTC01", "DTP04"],
        ["CLAUDE.md", ".claude/skills/pre-commit/SKILL.md"],
        "Step-ordering conflict: the always-on file prescribes lint-then-test, the "
        "skill body prescribes test-then-lint - both active when the skill fires.",
    ),
    _case(
        "proc-skill-sequence",
        {
            "CLAUDE.md": (
                "# Skills\n\n"
                "For release work, invoke the changelog skill first and the "
                "version-bump skill second - the changelog needs the pre-bump diff.\n"
            ),
            ".claude/skills/version-bump/SKILL.md": (
                "---\n"
                "name: version-bump\n"
                "description: Use when cutting a release to update version numbers.\n"
                "---\n"
                "# Version bump\n\n"
                "Bump the version before anything else touches the release - the "
                "changelog and tags key off the new number, so run this skill ahead "
                "of the changelog step.\n"
            ),
        },
        ["DTC02", "DTC01", "DTP04"],
        ["CLAUDE.md", ".claude/skills/version-bump/SKILL.md"],
        "Skill-invocation order conflict: the orchestrating file says changelog "
        "before bump; the skill's own body claims it must run first.",
    ),
    _case(
        "proc-main-vs-skill-field",
        {
            "CLAUDE.md": (
                "# Conventions\n\n"
                'Commit subjects are written in the imperative mood ("Add parser", '
                'not "Added parser").\n'
            ),
            ".claude/skills/release-commit/SKILL.md": (
                "---\n"
                "name: release-commit\n"
                "description: Use when writing the release commit and its message.\n"
                "---\n"
                "# Release commits\n\n"
                'Phrase the subject in past tense ("Added parser support") so the '
                "changelog reads chronologically.\n"
            ),
        },
        ["DTC01", "DTC02", "DTP04"],
        ["CLAUDE.md", ".claude/skills/release-commit/SKILL.md"],
        "The same field (commit subject mood) is prescribed differently by the "
        "always-on file and a conditionally-loaded skill body.",
    ),
    _case(
        "proc-approval-gate",
        {
            "CLAUDE.md": (
                "# Safety\n\n"
                "Any command that mutates infrastructure needs a human sign-off "
                "before it runs. No exceptions during business hours.\n"
            ),
            ".claude/skills/auto-remediate/SKILL.md": (
                "---\n"
                "name: auto-remediate\n"
                "description: Use when an alert fires and the runbook has a known fix.\n"
                "---\n"
                "# Auto-remediation\n\n"
                "Apply the runbook fix immediately and page the on-call afterwards - "
                "waiting on approval defeats the point of automated remediation.\n"
            ),
        },
        ["DTC02", "DTC01", "DTP04", "DTC05"],
        ["CLAUDE.md", ".claude/skills/auto-remediate/SKILL.md"],
        "Procedural gate conflict: the always-on file requires human approval "
        "before mutating actions; the skill instructs acting first and notifying "
        "after.",
    ),
]


# ---------------------------------------------------------------------------
# Benign-but-tricky trees (no conflict-class code may fire)
# ---------------------------------------------------------------------------

BENIGN_CASES: list[Case] = [
    _benign(
        "benign-negation-dense",
        {
            "CLAUDE.md": """\
# Safety

Never commit secrets or API keys.
Don't log bearer tokens, even truncated ones.
Avoid printing environment variables in CI output.
Do not disable the pre-commit hooks to get around a failure.
""",
        },
        "four compatible prohibitions about adjacent objects, negation-dense",
    ),
    _benign(
        "benign-exception-carveout",
        {
            "CLAUDE.md": """\
# Git

Never commit directly to the main branch.
Commit directly to main only when reverting a broken deploy, and tell the
team when you do.
""",
        },
        "broad prohibition with an explicit only-when carve-out the author "
        "considers part of the same rule",
    ),
    _benign(
        "benign-numeric-band",
        {
            "CLAUDE.md": """\
# Module size

Aim to keep modules under 500 lines; split anything that grows past 800 into
smaller files.
""",
        },
        "a soft target and a hard split point form a band, not a conflict",
    ),
    _benign(
        "benign-two-knobs",
        {
            "CLAUDE.md": """\
# Formatting

Wrap code at 100 characters.
Keep commit subject lines to 72 characters.
""",
        },
        "two character limits about different knobs (code lines vs commit "
        "subjects), jointly satisfiable",
    ),
    _benign(
        "benign-scope-split-paraphrase",
        {
            "CLAUDE.md": """\
# Style

Use double quotes for strings in TypeScript files.
""",
            ".claude/rules/frontend.md": """\
---
paths: "web/**"
---
Stick to double quotes for strings.
""",
            "web/src/app.tsx": "export const App = () => null;\n",
        },
        "same prescription paraphrased under a narrower scope — redundancy at "
        "worst, never a conflict",
    ),
    _benign(
        "benign-permit-plus-oblige",
        {
            "CLAUDE.md": """\
# Git

You may force-push branches that only you are working on.
Always coordinate in the team channel before force-pushing anything shared.
""",
        },
        "a permission for private branches plus an obligation for shared ones",
    ),
    _benign(
        "benign-retry-plus-wait",
        {
            "CLAUDE.md": """\
# Testing

Retry flaky integration tests up to 3 times.
Wait 30 seconds between retries so the container has time to settle.
""",
        },
        "a retry count and a backoff duration — different dimensions, one policy",
    ),
    _benign(
        "benign-ordering",
        {
            "CLAUDE.md": """\
# Workflow

Run the formatter before committing.
After committing, push the branch and open a pull request.
""",
        },
        "before-X and after-X steps of one pipeline, fully compatible",
    ),
    _benign(
        "benign-unless-hedge",
        {
            "CLAUDE.md": """\
# Dependencies

Don't pull in new dependencies unless the standard library genuinely can't do
the job.
Prefer the standard library where it's practical.
""",
        },
        "a hedged prohibition and the preference it restates",
    ),
    _benign(
        "benign-same-verb-different-objects",
        {
            "CLAUDE.md": """\
# Data and branches

Never delete customer data, even in staging.
Delete stale feature branches once they've merged.
""",
        },
        "same verb, opposite modality, different objects — compatible",
    ),
    _benign(
        "benign-scoped-formats",
        {
            "CLAUDE.md": """\
# Output formats

Respond with JSON when you call the summarize endpoint.
Write pull request descriptions in Markdown.
""",
        },
        "two format rules for different outputs, not one exclusive channel",
    ),
    _benign(
        "benign-known-exception",
        {
            "CLAUDE.md": """\
# Module size

Most modules should stay under 400 lines; the parser is a known exception and
can stay as it is.
""",
        },
        "a quantified guideline with a named legacy exception",
    ),
    _benign(
        "benign-passive-paraphrase",
        {
            "CLAUDE.md": """\
# Testing

Tests are run before every push.
""",
            "AGENTS.md": """\
# Agent guide

Make sure the test suite passes before pushing.
""",
        },
        "passive restatement of the same rule across files — redundancy at worst, never a conflict",
    ),
    _benign(
        "benign-makefile-tabs",
        {
            "CLAUDE.md": """\
# Style

Indent Python with 4 spaces.
""",
            ".claude/rules/make.md": """\
---
paths: "Makefile"
---
Recipes in Makefiles are indented with hard tabs — that's a make requirement,
not a style call.
""",
            "Makefile": "test:\n\tpytest\n",
        },
        "indentation rules for different languages under different scopes",
    ),
    _benign(
        "benign-migrations-vs-backfills",
        {
            "CLAUDE.md": """\
# Database changes

It's best if schema changes go through the migration tool rather than
hand-written SQL.
Hand-written SQL is fine for one-off data backfills — just get it reviewed
first.
""",
        },
        "hedged preference split by object: schema changes vs data backfills",
    ),
    _benign(
        "benign-mocks-by-layer",
        {
            "CLAUDE.md": """\
# Testing

Don't mock the database in integration tests; run against the compose
Postgres.
Do mock external HTTP APIs in unit tests so they stay fast and offline.
""",
        },
        "mocking forbidden in one test layer and required in another",
    ),
    _benign(
        "benign-fence-numbers",
        {
            "CLAUDE.md": """\
# Webhooks

Retry failed webhook deliveries up to 3 times.

The worker config mirrors that:

```yaml
webhooks:
  retries: 3
  backoff_seconds: 30
```
""",
        },
        "prose retry rule alongside the matching numbers inside a code fence",
    ),
    _benign(
        "benign-proc-scoped-order",
        {
            "CLAUDE.md": (
                "# Workflow\n\nFor Python changes, run the type checker before the tests.\n"
            ),
            ".claude/skills/frontend-build/SKILL.md": (
                "---\n"
                "name: frontend-build\n"
                "description: Use when building or debugging the frontend bundle.\n"
                "---\n"
                "# Frontend builds\n\n"
                "Run the bundler before the tests - the suite imports built assets.\n"
            ),
        },
        "Different pipelines (Python type-check ordering vs frontend bundle "
        "ordering) - no shared step, no conflict.",
    ),
    _benign(
        "benign-proc-refinement",
        {
            "CLAUDE.md": ("# Releases\n\nCut releases from the main branch after CI is green.\n"),
            ".claude/skills/hotfix/SKILL.md": (
                "---\n"
                "name: hotfix\n"
                "description: Use for emergency hotfix releases from a release branch.\n"
                "---\n"
                "# Hotfixes\n\n"
                "Hotfixes branch from the latest release tag, not main; CI must still "
                "be green before tagging.\n"
            ),
        },
        "The skill narrows the release procedure for a distinct emergency case "
        "and keeps the CI gate - a refinement, not a conflict.",
    ),
]
