---
name: hotfix
description: Use when shipping a new version of Orbit to production urgently — cut the fix, tag it, publish the packages, and roll the deploy out ahead of the release train.
---

# Hotfix path

For P0 and P1 incidents only. Everything else rides the weekly train, no
matter how tempting the shortcut looks.

## Steps

1. Branch from the latest production tag, not from main; the fix must ship on
   what customers are running.
2. Cherry-pick or write the minimal fix. No drive-by refactors, no dependency
   bumps, nothing the incident does not require.
3. Keep the branch rebased on the production tag while the fix is in review,
   so the diff stays exactly the incident fix.
4. Force-push the branch after each rebase; a force-push is required here
   because the rebase rewrites every commit on the branch.
5. Tag, publish, and roll out through the canary pipeline, same as a normal
   release.
6. Watch the canary for at least 10 minutes with the incident dashboard open;
   a hotfix trades bake time for speed, so the watching is on you.
7. After the fleet is healthy, merge the fix back to main through a normal
   PR so the next train carries the incident fix too.
