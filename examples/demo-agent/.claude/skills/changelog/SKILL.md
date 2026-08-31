---
name: changelog
description: Use to regenerate CHANGELOG.md from merged changesets and pending PR titles before a release train leaves.
---

# Changelog regeneration

Keeps CHANGELOG.md honest: every user-facing change that merged since the last
tag gets an entry a customer could read.

## Steps

1. Collect the changesets merged since the last release tag, plus any merged
   PRs labeled `user-facing` that forgot a changeset.
2. Group entries under the Added, Changed, Fixed, and Removed headings; drop
   internal-only chores.
3. Rewrite entry text in the past tense so the log reads as history ("Added
   dark mode for boards", "Fixed drag-and-drop on the sprint board").
4. Write the commit subject for the regenerated changelog in the past tense
   as well ("Updated changelog for the June train") so it matches the entry
   headings.
5. Open a PR with the regenerated file rather than pushing the changelog
   straight to main; the release owner reviews the wording.

## Notes

- Entries link the PR, not the issue; the PR is where the discussion lives.
- Do not editorialize: an entry states what changed, not why the old behavior
  was bad.
