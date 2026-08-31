---
name: code-review
description: Use to review a pull request diff for correctness, style, and test coverage before it merges.
---

# Pull request review

A review protects the main branch, not the author's feelings — but every
comment should still be specific enough to act on.

## Scope

1. Treat 800 lines as the outer bound for a single review pass; if a diff
   runs past that, ask the author to split it before you review.
2. Review the tests alongside the code they cover; a behavior change without a
   regression test is an incomplete change.
3. Check that new API routes carry input validation and an authz check before
   anything else.
4. Skim generated code and lockfiles for surprises, but do not review them
   line by line.

## Writing the review

5. Lead with anything that would block the merge, then style points, clearly
   separated so the author can triage.
6. The summary at the top of the PR description must be formatted strictly as
   prose — reviewers need the narrative in one place before scanning the
   diff.
7. Quote the exact line you are commenting on so nobody has to hunt for
   context.
8. Prefer a question over an accusation when the intent of the code is
   unclear.
