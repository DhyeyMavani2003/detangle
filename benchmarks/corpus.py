"""Clean, realistic base configuration trees for the seeded-conflict benchmark.

Each tree is a ``{repo-relative path: file text}`` dict that materializes to a
small but plausible repository. The contract (enforced by
tests/test_benchmark.py and re-checked by ``run_eval``):

- every tree scans **clean**: zero error-severity findings and zero
  conflict-class findings (DTC01–05, DTP01–04) with the deterministic lanes;
- every tree carries the hooks the mutation operators need: at least one
  ``- Always/Never <verb> …`` obligation in an always-on file, one bounded
  numeric constraint with digits, a model-triggered surface (or room to add
  one), and non-config source files for glob targeting.

Mutators then inject exactly one labeled defect per run, so any conflict-class
finding on a mutated tree is attributable to the injection.
"""

from __future__ import annotations

CLAUDE_TREE: dict[str, str] = {
    "CLAUDE.md": """\
# Storefront API — project guide

## Git workflow
- Never commit directly to the main branch; open a pull request instead.
- Always run the full test suite before opening a pull request.
- Write commit messages in the imperative mood.

## Testing
- Use pytest fixtures for setup instead of ad-hoc helper functions.
- Retry flaky integration tests at most 2 times before investigating.

## Dependencies
- Pin new Python dependencies to exact versions in requirements.txt.
""",
    ".claude/rules/git-hygiene.md": """\
- Rebase feature branches on main instead of merging main into them.
- Squash fixup commits before requesting review.
""",
    ".claude/rules/api-conventions.md": """\
---
paths: "src/api/**"
---
- Validate request payloads with Pydantic models at the route boundary.
- Return a structured problem document for every error response.
""",
    ".claude/skills/changelog/SKILL.md": """\
---
name: changelog
description: Use when the user asks to update the changelog, draft release notes, or summarize merged pull requests for a release entry.
---
# Changelog updates

Read the merged pull requests since the last release tag and group them into
Added, Changed, and Fixed sections. Keep each entry on a single line and link
the pull request number.
""",
    ".claude/skills/perf-audit/SKILL.md": """\
---
name: perf-audit
description: Use when an endpoint is slow or the user asks to profile database queries and cache behaviour.
---
# Performance audit

Profile the slow endpoint first, then inspect its heaviest SQL queries and
cache hit rates. Present findings as a table of endpoint, latency, and query
count before proposing a fix.
""",
    "src/api/routes.py": '"""Route handlers for the storefront API."""\n',
    "src/api/models.py": '"""Pydantic request and response models."""\n',
    "requirements.txt": "fastapi==0.115.0\npydantic==2.8.2\n",
}


AGENTS_TREE: dict[str, str] = {
    "AGENTS.md": """\
# Shop monorepo — agent guide

The checkout API lives in services/api and the storefront lives in web.

## Working agreements
- Run `npm run lint` before every commit.
- Never force-push shared branches; revert with a new commit instead.
- Always update the OpenAPI spec when an endpoint changes.
- Keep pull requests under 400 lines of diff.
""",
    "services/api/AGENTS.md": """\
# Checkout API

- Add a database migration for every schema change.
- Write integration tests against the compose Postgres container, not against mocks.
""",
    "web/AGENTS.md": """\
# Storefront

- Co-locate component styles with the component file.
- Prefer server components; reach for client components only for stateful widgets.
""",
    "services/api/app.py": '"""Checkout API entry point."""\n',
    "services/api/migrations/0001_init.sql": "-- initial schema\n",
    "web/src/index.tsx": "export {};\n",
    "package.json": """\
{
  "name": "shop-monorepo",
  "private": true,
  "scripts": {
    "lint": "eslint .",
    "test": "vitest run"
  }
}
""",
}


CURSOR_TREE: dict[str, str] = {
    ".cursor/rules/core.mdc": """\
---
alwaysApply: true
---
- Use TypeScript strict mode for all new files.
- Always ask before adding a new runtime dependency.
- Always write unit tests for exported hooks.
- Keep source files under 300 lines.
""",
    ".cursor/rules/components.mdc": """\
---
globs: "src/components/**"
---
- Export a single component per file.
- Style components with Tailwind utility classes rather than inline style objects.
""",
    ".cursor/rules/database.mdc": """\
---
description: Apply when writing SQL migrations or changing the database schema.
---
- Wrap every migration in a transaction.
- Name migrations with a zero-padded sequence number prefix.
""",
    "src/components/Button.tsx": "export const Button = () => null;\n",
    "src/lib/db.ts": "export const db = {};\n",
    "migrations/0001_init.sql": "-- initial schema\n",
}


MIXED_TREE: dict[str, str] = {
    "CLAUDE.md": """\
# Billing platform — agent guide

## Git
- Never rewrite published history on shared branches.
- Always request review from the billing team for schema changes.

## Code review
- Keep pull request descriptions under 200 words.

## Safety
- Ask before running commands that modify production data.
""",
    "services/billing/AGENTS.md": """\
# Billing worker

- Record every ledger mutation in the audit log.
- Use integer cents for all monetary amounts.
""",
    ".github/instructions/frontend.instructions.md": """\
---
applyTo: "web/**"
---
- Build UI states for loading, empty, and error cases in every view.
- Keep translations in the locale files rather than hard-coded strings.
""",
    ".claude/skills/oncall-runbook/SKILL.md": """\
---
name: oncall-runbook
description: Use when the user reports a production incident or asks how to page the on-call engineer.
---
# On-call runbook

Check the incident channel first, capture the alert link, and identify the
failing service before paging anyone. Escalate to the on-call engineer when
a customer-facing charge fails.
""",
    "services/billing/worker.py": '"""Billing worker entry point."""\n',
    "web/src/App.tsx": "export const App = () => null;\n",
    "web/locales/en.json": '{"checkout": "Checkout"}\n',
}


#: name -> tree. Names are stable identifiers used in eval reports and tests.
TREES: dict[str, dict[str, str]] = {
    "claude-webapp": CLAUDE_TREE,
    "agents-monorepo": AGENTS_TREE,
    "cursor-spa": CURSOR_TREE,
    "mixed-stack": MIXED_TREE,
}
