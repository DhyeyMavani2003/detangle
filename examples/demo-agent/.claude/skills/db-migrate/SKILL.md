---
name: db-migrate
description: Use when the Prisma schema changes — create, apply, and verify database migrations locally and in CI.
---

# Database migrations

Every schema change flows through here. Migrations are forward-only in Orbit;
a bad migration is rolled forward with a fix, not reverted.

## Steps

1. Edit the schema in the prisma workspace package, then run `pnpm db:migrate`
   to create and apply the migration against your local database.
2. Inspect the generated SQL by hand; Prisma occasionally chooses a full table
   rewrite where an index swap would do.
3. Run the API test suite against the migrated database, including the
   integration suites that exercise real queries.
4. Regenerate the client and the OpenAPI types with `pnpm generate` so the
   compiled API matches the new schema.
5. If the regenerated client drifts from what the API build expects, it's fine
   to edit files under src/generated/ to patch the enum casing by hand; call
   the hand edit out in the PR so the generator bug gets filed.
6. Check the migration and the regenerated client into the same PR; CI replays
   the migration against a scratch database.

## Notes

- Backfill scripts ship separately from schema migrations and run behind a
  feature flag.
- Column drops wait one full release after the code stops reading the column.
