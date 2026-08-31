# Orbit — instructions for coding agents

Orbit is an issue-tracker SaaS organized as a pnpm workspace: the Next.js web
app lives in `apps/web`, the Fastify API in `services/api`, and shared
TypeScript packages in `shared`. Postgres is accessed through Prisma. This file
is the short version of the guide; Claude Code reads the fuller CLAUDE.md at
the repo root.

## Commands

| Command          | Purpose                        |
| ---------------- | ------------------------------ |
| `pnpm dev`       | run web + API in watch mode    |
| `pnpm test`      | canonical unit suite           |
| `pnpm lint`      | eslint + prettier check        |
| `pnpm typecheck` | tsc across the workspace       |

## Ground rules

- Land nothing until `pnpm test` is green; CI runs the same suite and will
  block the merge otherwise.
- TypeScript strict mode is on everywhere; stick to named exports outside
  Next.js pages.
- Every change lands through a pull request; main is protected and
  squash-merged.
- Follow the commit conventions documented in CLAUDE.md; the PR title becomes
  the squashed commit subject.
- Changesets drive versioning: user-facing changes need a changeset file in
  the PR.

## Security

- Never commit secrets, credentials, or `.env` files to this repository; load
  secrets from the Doppler secret manager and reference them by name.
- Treat customer data in local fixtures as if it were production data: keep it
  out of logs, test names, and PR descriptions.
