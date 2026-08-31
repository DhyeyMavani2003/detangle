# EXPECTED — seeded-truth manifest for the demo-agent fixture

This directory is a deliberately conflicted agent configuration for a fictional
TypeScript SaaS ("Orbit"). It exists so detangle can be exercised — and
dogfooded in CI — against a realistic corpus with known ground truth. Every
conflict below was planted on purpose; everything not listed here is intended
to be consistent. Line numbers refer to the files as committed; re-derive them
after editing any fixture file.

## Planted conflicts (14)

| ID  | Class                       | Side A                                   | Side B                                          | Why it conflicts |
| --- | --------------------------- | ---------------------------------------- | ----------------------------------------------- | ---------------- |
| C1  | Procedural / order          | CLAUDE.md:27-29 (changelog before release) | .claude/skills/release/SKILL.md:18-20 (notes first, backfill CHANGELOG.md last) | CLAUDE.md orders changelog → release; the release skill drafts the release first and backfills the changelog at the end. |
| C2  | Procedural / order          | CLAUDE.md:25-26 (pre-commit lints first, then typechecks, then tests) | .claude/skills/pre-commit/SKILL.md:14-23 (steps: tests, typecheck, lint last) | The main file and the skill body prescribe opposite gate orders for the same gauntlet. |
| C3  | Procedural / order          | CLAUDE.md:30-32 (db-migrate completes before deploy starts) | .claude/skills/deploy/SKILL.md:15-20 (canary deploy first, migrations after canary is healthy) | Migrate-then-deploy vs deploy-then-migrate cannot both govern one rollout. |
| C4  | Cross-layer field treatment | CLAUDE.md:65-66 (commit subjects imperative mood) | .claude/skills/changelog/SKILL.md:19-21 (commit subject in the past tense) | The skill instructs past-tense commit subjects for changelog commits; CLAUDE.md mandates imperative mood for all commits. |
| C5  | Cross-layer permit/forbid   | CLAUDE.md:72-73 ("Never force-push.")    | .claude/skills/hotfix/SKILL.md:19-20 (force-push required after each rebase) | An unconditional prohibition vs a skill step that requires the forbidden action. |
| C6  | Numeric                     | CLAUDE.md:68-69 (hard cap 400 lines of diff per PR) | .claude/skills/code-review/SKILL.md:13-14 (800 lines reviewable in one pass, split beyond) | Two different ceilings for the same quantity: a 401–800-line PR is illegal by one rule and routine by the other. |
| C7  | Numeric                     | CLAUDE.md:54-55 (vitest per-test timeout 30 seconds) | .claude/rules/backend.md:18-20 (timeout at least 120 seconds, tests take up to two minutes) | 30s and ≥120s for the vitest timeout cannot both hold when working under services/api/**. |
| C8  | Output format               | CLAUDE.md:70-71 (PR descriptions as bullet points only) | .claude/skills/code-review/SKILL.md:26-28 (summary formatted strictly as prose) | Bullets-only vs prose-summary are mutually exclusive shapes for the same PR description. |
| C9  | Permit vs forbid            | CLAUDE.md:47-48 (never edit files under src/generated/) | .claude/skills/db-migrate/SKILL.md:21-23 (it's fine to edit files under src/generated/ on drift) | The skill permits hand-editing exactly what the main file forbids touching. |
| C10 | Duplicate                   | CLAUDE.md:74-75 (never commit secrets/.env) | AGENTS.md:33-34 (same rule, near-verbatim)      | The same secrets rule stated twice across surfaces — copies that can silently diverge. |
| C11 | Drifted near-duplicate      | CLAUDE.md:52-53 (`pnpm test` before opening a PR) | .claude/skills/pre-commit/SKILL.md:14-17 (`pnpm run test:unit --coverage`, described as the same canonical suite) | One instruction, two diverging command spellings for "the canonical suite". |
| C12 | Stale reference             | CLAUDE.md:20-21 (scripts/verify-all.sh)  | CLAUDE.md:60-61 (docs/testing.md)               | Neither referenced file exists anywhere in the tree. Two findings expected. |
| C13 | Routing ambiguity           | .claude/skills/release/SKILL.md:3 (description) | .claude/skills/hotfix/SKILL.md:3 (description)  | Both descriptions claim "shipping a new version of Orbit to production"; the router has no documented arbitration between them. |
| C14 | Shadow / precedence         | .claude/rules/frontend.md:1-4,15-17 (paths include shared/**; imports sorted in one flat alphabetical block) | .claude/rules/backend.md:1-4,15-17 (paths include shared/**; imports grouped by layer, then alphabetized) | Both path-scoped rules claim shared/** and prescribe incompatible import orders there, with no declared winner. |

## Deterministic-lane expectations

The deterministic lane (plain `detangle scan`, no LLM lanes) is expected to
catch **9 of the 14** — C5 (DTP04), C6 (DTC03), C7 (DTC03), C8 (DTC04),
C9 (DTP04), C10 (DTR01), C11 (DTR02), C12 (DTR05 ×2), C13 (DTS01) — and to
report **zero** findings on the benign traps below. C1–C4 (order and mood
conflicts) and C14 (semantic import-order clash) need the NLI/jury lanes:
their sentences share no frame, antonym, or numeric handle a precision-first
deterministic rule may fire on.

## Benign traps (must produce NO findings)

| Trap | Where | Why it is not a conflict |
| ---- | ----- | ------------------------ |
| T1 target vs ceiling | CLAUDE.md:68-69 | "Aim for under 200 changed lines" and "hard cap 400" are a soft target and a hard limit in the same sentence — a band, not a contradiction. |
| T2 scoped format exception | CLAUDE.md:44-46 vs CLAUDE.md:70-71 | API reference pages use tables; PR descriptions use bullets. Different artifacts, different scopes — no overlap. |
| T3 do-X-instead refinement | .claude/rules/frontend.md:13-14 | "Use `apiFetch` instead of calling `axios` directly" replaces one mechanism with another; nothing elsewhere prescribes calling axios. |
| T4 unless carve-out | .claude/skills/deploy/SKILL.md:23 | "Never start a production deploy on a Friday unless a P0 incident is open" is a single rule with its own exception, not two clashing rules. |

## Expected incidental findings (not planted, structurally true)

Because this fixture ships only configuration (no application source), a scan
of this directory also reports, correctly:

- DTP06 dead scope ×2 — frontend.md and backend.md globs match no file in this
  tree (there is no apps/web/ or services/api/ here).
- DTP05 divergent interpretation ×2 — AGENTS.md is intentionally the short
  parallel of CLAUDE.md (similarity below the mirror threshold), and Zed would
  read only AGENTS.md.

These four are properties of the fixture's shape, not seeded conflicts; keep
them out of precision/recall accounting for the 14 above.
