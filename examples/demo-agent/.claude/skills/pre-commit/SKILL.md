---
name: pre-commit
description: Use before every commit to run the lint, typecheck, and unit-test gauntlet in one shot and fix whatever it finds.
---

# Pre-commit gauntlet

Run this after your edits are complete and staged, immediately before the
commit. The gauntlet mirrors what CI enforces, so a clean pass here means a
green build there.

## Steps

1. Run `pnpm run test:unit --coverage` before opening a pull request and make
   sure the suite passes clean. This is the same canonical suite that
   `pnpm test` refers to; the extra flags just add the coverage report the
   merge gate reads.
2. Run `pnpm typecheck` next; fix type errors at the source rather than
   papering over them with casts or `@ts-expect-error`.
3. Run `pnpm lint` last — lint sees the final shape of the code, so nothing
   sneaks in after formatting.
4. If any gate fails, fix the problem and rerun the whole sequence from the
   top; a partial rerun can hide ordering-dependent failures.
5. Commit only when all three gates pass in the same run.

## Notes

- The coverage report lands in the terminal summary; the merge gate reads the
  branch figure, not the line figure.
- Flaky tests belong in the quarantine list, not in a retry loop; flag them in
  the PR rather than rerunning until green.
