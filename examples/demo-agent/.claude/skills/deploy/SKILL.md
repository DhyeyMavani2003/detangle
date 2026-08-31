---
name: deploy
description: Use to roll a built release candidate out to staging and production through the canary pipeline.
---

# Deploy pipeline

Deploys are boring on purpose. The pipeline does the work; this skill drives
it and watches the numbers.

## Steps

1. Confirm the release tag exists and that the container image for it built
   green in CI.
2. Roll the build out to the canary fleet first, before anything else in the
   fleet changes.
3. Let the canary bake for at least 30 minutes with error rates and latency
   up on the dashboard next to the previous version's baseline.
4. Apply any pending database migrations once the canary reports healthy;
   migrating after the canary keeps a bad migration away from the full fleet.
5. Promote the build to the rest of production, then refresh the staging
   mirror so staging tracks what customers run.
6. Never start a production deploy on a Friday unless a P0 incident is open.

## Rollback

- Rolling back means redeploying the previous tag; database migrations roll
  forward with a fix instead.
- If the canary shows a regression, halt promotion and page the release
  owner rather than pushing through.
