# Orbit — agent development guide

Orbit is an issue-tracker SaaS built as a pnpm workspace: a Next.js web app in
`apps/web`, a Fastify API in `services/api`, and shared TypeScript packages in
`shared`. Postgres through Prisma, vitest for unit tests, Playwright for
end-to-end, changesets for versioning. Node 20 is pinned in `.nvmrc`; enable
corepack rather than installing pnpm globally.

## Dev commands

| Command          | What it does                                    |
| ---------------- | ----------------------------------------------- |
| `pnpm dev`       | web app + API in watch mode                     |
| `pnpm test`      | the canonical test suite (unit, all workspaces) |
| `pnpm test:e2e`  | Playwright suite against a local stack          |
| `pnpm lint`      | eslint + prettier check                         |
| `pnpm typecheck` | tsc across the workspace                        |
| `pnpm db:migrate`| apply Prisma migrations locally                 |

Run `scripts/verify-all.sh` before you push; it chains lint, typecheck, and the
unit suite in the exact order CI uses.

## Workflow — which skill, in what order

1. Everyday changes: edit, then invoke the pre-commit skill as the last step
   before any commit; it lints first, then typechecks, then runs the unit suite.
2. Shipping: run the changelog skill before the release skill so the release
   notes are assembled from an up-to-date CHANGELOG.md, never the other way
   around.
3. Schema changes: run the db-migrate skill to completion before starting the
   deploy skill. A deploy with pending migrations is a paging incident waiting
   to happen.
4. Reviews: the code-review skill handles routine PR review; hand risky or
   cross-cutting diffs to the reviewer subagent.
5. Emergencies: the hotfix skill is for P0/P1 incidents only; everything else
   rides the weekly release train.

## Code style

- TypeScript strict mode everywhere; no `any` outside test helpers.
- Use named exports; default exports are reserved for Next.js pages and API
  route modules.
- Prefer small, single-purpose modules over utility grab-bags.
- Endpoint reference pages in the API docs are written as tables, one row per
  route; the bullet-list convention below stops at generated reference
  material.
- Never edit files under src/generated/ — the Prisma client and the OpenAPI
  types there are rebuilt on every build, and hand edits are silently lost.

## Testing

- Run `pnpm test` before opening a pull request and make sure the suite passes
  clean.
- Keep the vitest per-test timeout at 30 seconds; a unit test that needs longer
  is doing too much.
- Keep branch coverage at or above 80% — the coverage gate blocks merges under
  the floor.
- New Playwright specs go next to the flow they cover and must run green
  locally before review.
- See docs/testing.md for the flake quarantine process and for what counts as
  an integration test.

## Git and pull requests

- Write commit subjects in the imperative mood ("add board filters", not
  "added board filters") and keep them at most 72 characters.
- Prefix subjects with the affected area: `web:`, `api:`, or `shared:`.
- Keep pull requests small — aim for under 200 changed lines; the hard cap is
  400 lines of diff per PR.
- Always write PR descriptions as bullet points only — one bullet per
  meaningful change, with a link to the Linear issue in the first bullet.
- Never force-push. If a shared branch truly needs rewriting, delete it and
  re-cut it from main.
- Never commit secrets, credentials, or `.env` files to the repository; load
  secrets from the Doppler secret manager and reference them by name.
- Squash-merge only; the PR title becomes the squashed commit subject.

## Permissions

- You may run the dev servers, the test suites, and read-only queries against
  your local Postgres without asking.
- You may add workspace dependencies with `pnpm add` when a task needs them;
  call new dependencies out in the PR description.
- Never push directly to main; every change lands through a pull request.
- Never run destructive commands against the staging or production databases;
  migrations reach those environments only through the deploy pipeline.
- Ask before deleting files you did not create or touching anything under
  `.github`.
