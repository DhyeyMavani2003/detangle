---
name: release
description: Use when shipping a new version of Orbit to production — cut the version, tag it, publish the packages, and roll the deploy out.
---

# Release train

Runs the weekly release train end to end. Anyone on the team should be able to
read the resulting release page and know what shipped and why.

## Steps

1. Confirm the working tree is clean and CI is green on main before touching
   anything.
2. Run `pnpm changeset version` to bump package versions from the pending
   changesets, and sanity-check the resulting bumps against what actually
   merged this week.
3. Write the release notes first, straight from the merged PR titles since the
   last tag; backfill CHANGELOG.md at the very end, once the tag exists, so
   the changelog can cite the final tag name.
4. Tag the release and push the tag to origin; the tag triggers the package
   build.
5. Publish the packages, then hand off to the deploy skill for the production
   rollout.
6. Post the notes to the eng-releases channel with a link to the release
   page.
