# Ecosystem semantics: what detangle models, and why findings say what they say

A conflict only matters if both instructions can be in the model's context at the same time —
and it only *stays* a conflict if nothing documented resolves it. So every pairwise finding
carries two accounts:

- a **co-activation account** — *can these two units co-load, and under what conditions?*
- a **precedence account** — *if they co-load and disagree, does the ecosystem declare a
  winner?*

This page documents the per-ecosystem discovery, activation, merge, and precedence semantics
those accounts are built from. All semantics were verified against vendor documentation
(2026-08); where a vendor is quoted, the quote is attributed. Where the answer is genuinely
**UNSPECIFIED** in the vendor's documentation, detangle does not invent a resolution rule — it
flags the ambiguity (that is the point of the tool).

## The four precedence answers

Every disagreeing pair gets exactly one of:

| Kind | Meaning | Consequence |
|---|---|---|
| `resolved` | A declared hierarchy picks a winner (e.g. skill name-shadowing across levels) | **No conflict finding** — working as intended (shadowing/fragility may still be flagged) |
| `positional` | Only concatenation order decides ("later wins", soft) | Finding notes the order is an accident, not a contract (DTP03 territory) |
| `ambiguous` | Co-equal, no declared order | Conflict findings fire at full strength |
| `undocumented` | The ecosystem simply does not specify resolution (all cross-mechanism and cross-ecosystem pairs) | Routed to DTP04 cross-layer-conflict |

## The five co-activation classes

| Class | Meaning | Exposure weight |
|---|---|---|
| `always-always` | Both in the launch set | 1.0 |
| `always-conditional` | One always loaded; the other glob-, description-, or user-triggered | 0.7 |
| `conditional-overlapping` | Both conditional, and the conditions can co-fire (globs intersect; descriptions can co-trigger) | 0.5 |
| `cross-tool-only` | No single tool reads both files — they co-apply only in the sense that different tools serve the same repo | 0.3 |
| `mutually-exclusive` | Provably never co-active (disjoint globs; isolated contexts) | pruned before detection |

Exposure feeds severity: e.g. a direct contradiction between two launch-set files is an
`error`; the same contradiction where one side is conditionally loaded is a `warning`.

---

## Claude Code

Claude Code is not one precedence regime — it is at least three, with **opposite polarity
between skills and subagents**. detangle applies a per-mechanism precedence table, never a
global one.

### Memory (`CLAUDE.md` hierarchy)

- **Discovery:** project root `CLAUDE.md` or `.claude/CLAUDE.md`, plus `CLAUDE.local.md`
  (loads *alongside* — not deprecated), plus subdirectory `CLAUDE.md` files. A user-global
  `~/.claude/CLAUDE.md` layer is modeled when a simulated user directory is supplied
  (`Config.user_dir`, used by tests/CI).
- **Activation:** root-level files are in the launch set (`always`). Subdirectory `CLAUDE.md`
  loads **on demand** when files in that subtree are read — modeled as path-triggered over
  `subdir/**`.
- **`@imports`:** resolved relative to the importing file, max depth 4 hops, code spans
  skipped. detangle follows in-repo imports (each imported file becomes its own unit source
  carrying the importer's activation and tier) and notes imports it cannot follow (outside the
  repo, missing targets, depth exceeded).
- **Merge model:** **concatenation, root → cwd — never override.** Files closer to the launch
  directory are read *later*; earlier text still reaches the model and can win.
- **Conflict rule (documented, quoted in findings):** Anthropic — *"if two rules contradict
  each other, Claude may pick one arbitrarily."* detangle therefore classifies all
  memory-vs-memory disagreements as `ambiguous`.
- **Budgets:** Anthropic guidance is to target under 200 lines (detangle notes files above
  that). Comments: HTML comments are stripped before injection — which is also why a directive
  hidden in one is flagged (DTX01).
- **Readers:** `claude-code`; a repo-root `CLAUDE.md` is *also* read by Copilot (root only) —
  and by Zed, but only if nothing earlier in Zed's search list exists. Claude Code does **not**
  read `AGENTS.md` (feature request closed as unsupported).

### Rules (`.claude/rules/*.md`, recursive)

- Rules **without** `paths:` frontmatter load at launch (`always`); rules **with** `paths:`
  globs are path-triggered.
- Precedence: user rules load *before* project rules — positional priority only (project
  effectively higher). **Same-level ordering is unspecified** → disagreements between two
  project rules are `ambiguous`.

### Skills (`.claude/skills/*/SKILL.md`)

- **Activation:** model-triggered — the model routes on `description` + `when_to_use`. A skill
  with `disable-model-invocation: true` becomes user-invoked only.
- **Budget:** the combined description+when_to_use is truncated at **1,536 characters** in the
  skill listing — a skill over the cap gets a DTP06 finding, because its own *trigger* is
  partially invisible to the model. Bodies over 500 lines are noted (guidance ≤500;
  compaction re-attaches only the first 5,000 tokens).
- **Precedence:** name-shadowing across levels is deterministic — **enterprise > personal >
  project > plugin** — so cross-level same-name disagreements are `resolved`. But
  **co-triggering of *different* skills has no documented arbitration** → `ambiguous`, and
  overlapping trigger descriptions are DTS01.

### Subagents (`.claude/agents/*.md`)

- **Activation:** model-triggered via the frontmatter `description`.
- **The polarity flip:** subagent name precedence is **project > user** — the *opposite* of
  skills (personal > project). detangle encodes this per-mechanism; do not assume one ladder.
- **Context isolation:** each subagent body runs in its own context. Two different subagents'
  instructions are **never co-active** with each other (pairs pruned), and a subagent's units
  are not co-active with main-context skills/rules/commands — but they **are** co-active with
  the `CLAUDE.md` memory hierarchy, which subagents receive.
- Malformed/missing YAML frontmatter is noted: Claude Code silently skips such files.

### Commands (`.claude/commands/*.md`)

User-invoked (`/command`). A user-invoked unit can co-occur with anything, so command bodies
still participate in conflict detection at conditional exposure.

---

## The AGENTS.md family

One parser covers the `agents.md` standard and its many readers (Codex, Jules, Amp, OpenCode,
Copilot, Cursor-fallback, Zed) — because the *file format* is shared while the *semantics are
not*.

- **Discovery:** `AGENTS.md` at the root and in subdirectories; `AGENT.md` accepted as the
  legacy Amp name (noted).
- **Merge model — the standard's central gap:** "closest file takes precedence" is the only
  guidance, and **ancestor merge semantics are UNSPECIFIED**. Implementations verifiably
  diverge:
  - **Codex** concatenates root → cwd; precedence is explicitly positional — closer files
    override earlier guidance *"because they appear later in the combined prompt."* Earlier
    text still reaches the model.
  - **Copilot** applies nearest-wins.
  - **Zed** loads exactly one file (see the first-match list below).
  - **Jules** reads the root file only.

  detangle models the **Codex concatenation reading** — the most permissive with respect to
  co-activation — and flags material cross-tool divergence separately (DTP05). Same-level
  pairs are `ambiguous` ("the standard does not define merge semantics"); different-depth
  pairs are `positional`.
- **The Codex 32 KiB halt:** `project_doc_max_bytes` defaults to 32 KiB and discovery **halts
  at the limit** — deeper files are *silently dropped*, and the file that crosses the boundary
  is truncated mid-stream. detangle simulates the cumulative budget during ingestion and
  emits DTP06 (`unreachable-instruction`) for content past the halt point. This is modeled as
  unreachability, not override: nothing "wins", the text just never ships.
- **Readers:** `AGENTS.md` → codex, copilot, cursor, opencode, jules, amp (jules dropped for
  non-root files); `AGENT.md` → amp only.

---

## Cursor

- **Discovery:** only `.cursor/rules/*.mdc` files are parsed. A plain `.md` file inside
  `.cursor/rules/` is **ignored by Cursor entirely** — detangle flags it (DTP06 material): the
  file looks like configuration and never reaches the model. Legacy `.cursorrules` is
  deprecated but still read (always-on; also auto-detected by Cline behind a per-user toggle).
- **Four rule types** (by frontmatter):

  | Type | Trigger | Frontmatter |
  |---|---|---|
  | Always | always in context | `alwaysApply: true` (globs/description **ignored** — detangle notes when they are present anyway) |
  | Auto Attached | glob match | `globs:` |
  | Agent Requested | model-arbitrated | `description:` |
  | Manual | `@`-mention | none of the above |

- **Nesting:** `.cursor/rules/` directories in subdirectories scope their rules to that
  subtree regardless of type.
- **Precedence:** merge-all with **Team > Project > User** as *soft* priority ("earlier
  sources take precedence when guidance conflicts"). It is **not** nearest-wins.
  **Same-level ordering among matching rules is UNSPECIFIED** → detangle classifies Cursor
  rule disagreements as `ambiguous`. (Team-level rules live in Cursor's cloud, outside a repo
  scan.)
- Bodies over 500 lines are noted (vendor guidance).

---

## GitHub Copilot

- **Discovery:** `.github/copilot-instructions.md` (repo-wide, always in context) and
  `.github/instructions/*.instructions.md` with `applyTo` globs (path-scoped; without
  `applyTo`, always-on).
- **The union model:** precedence is Personal > repository > organization, **but** —
  quoting GitHub — *"all sets of relevant instructions are provided"* to the model.
  Everything co-activates; priority is advisory only. detangle therefore treats Copilot
  precedence as `ambiguous`: do **not** expect a personal instruction to shadow a repo one.
  The relative order of repo-wide vs path-specific instructions is UNSPECIFIED.
- **Budget:** coding-agent guidance is "no longer than 2 pages" — detangle notes files over
  roughly 1,000 words.
- Copilot also reads a repo-root `CLAUDE.md` and root/nested `AGENTS.md` (nearest-wins),
  which is how a CLAUDE.md instruction and an AGENTS.md instruction end up genuinely
  co-active under one tool — the pair is then cross-*surface*, with no documented precedence
  (DTP04/`undocumented`).

---

## Zed, and the readers matrix

**Zed reads exactly one file.** At the worktree root it takes the *first match* of this
ordered list and ignores the rest:

1. `.rules`
2. `.cursorrules`
3. `.windsurfrules`
4. `.clinerules`
5. `.github/copilot-instructions.md`
6. `AGENT.md`
7. `AGENTS.md`
8. `CLAUDE.md`
9. `GEMINI.md`

detangle applies this as a post-pass: only the winning file keeps `zed` in its reader set, and
a note records what Zed ignores. A repo where `.cursorrules` exists therefore has its
`CLAUDE.md` invisible to Zed — which is exactly the kind of cross-tool divergence DTP05
reports.

**The readers matrix** drives cross-tool co-activation: two files are co-active under tool T
only if T reads both. Surfaces detangle assigns per file:

| File | Read by |
|---|---|
| `CLAUDE.md` (repo root) | claude-code, copilot (+ zed if first match) |
| other `CLAUDE.md` / `.claude/**` (rules, skills, agents, commands) | claude-code |
| `AGENTS.md` (root) | codex, copilot, cursor, opencode, jules, amp (+ zed if first match) |
| `AGENTS.md` (nested) | codex, copilot, cursor, opencode, amp |
| `AGENT.md` | amp (+ zed if first match) |
| `.cursor/rules/*.mdc` | cursor |
| `.cursorrules` | cursor, cline (+ zed if first match) |
| `.github/copilot-instructions.md`, `.github/instructions/*` | copilot (+ zed for the former if first match) |

A pair of files with **no common reader** is classed `cross-tool-only` (exposure 0.3): the
conflict is real only in the sense that different tools serve the same repo different policies
— which is still worth knowing, and is what the finding's co-activation account will say.

---

## Condensed comparison

| Surface | Layers / discovery | Merge model | Documented conflict rule | Size budget modeled | detangle precedence class |
|---|---|---|---|---|---|
| Claude Code CLAUDE.md | user → project → local → subdir (+`@imports` ≤4 hops) | concatenate root→cwd | *"may pick one arbitrarily"* (explicit) | 200-line guidance (note) | `ambiguous` |
| Claude Code rules | `.claude/rules/**` | concat; user before project | positional across levels; same level unspecified | — | `positional` / `ambiguous` |
| Claude Code skills | `.claude/skills/*/SKILL.md` | name-shadowing | deterministic by level (ent > personal > project > plugin); co-trigger arbitration undocumented | 1,536-char listing cap; 500-line guidance | `resolved` (cross-level) / `ambiguous` (same level) |
| Claude Code subagents | `.claude/agents/*.md` | name-shadowing, **project > user** (polarity flip) | deterministic by level; bodies isolated | — | isolated contexts → pairs pruned |
| AGENTS.md (Codex reading) | root + nested | concatenate root→cwd | positional (*"appear later in the combined prompt"*) | **32 KiB discovery halt** | `positional` / `ambiguous` |
| Cursor rules | `.cursor/rules/*.mdc` (+ legacy `.cursorrules`) | merge-all; Team > Project > User soft | "earlier sources take precedence"; same-level unspecified | 500-line guidance | `ambiguous` |
| Copilot | `.github/copilot-instructions.md` + `instructions/*` | **union** — everything provided | soft priority only | "2 pages" guidance | `ambiguous` |
| Zed | one file, first match of 9-name list | n/a (single file) | n/a | — | readers-matrix post-pass |
| Cross-mechanism / cross-surface | any two of the above | side by side | **undocumented everywhere** | — | `undocumented` → DTP04 |

## The UNSPECIFIED list

These are verified *absent* from vendor documentation. detangle deliberately does not invent a
resolution rule for any of them — each is surfaced as a finding instead:

| Undefined behavior | How detangle flags it |
|---|---|
| AGENTS.md ancestor merge semantics (concat vs replace) | DTP05 divergent-interpretation; `positional` accounts note the divergence |
| Cursor same-level rule ordering | DTP02/DTC0x with an `ambiguous` precedence account |
| Copilot repo-wide vs path-specific relative order | `ambiguous` account on Copilot pairs |
| CLAUDE.md contradictions ("may pick one arbitrarily") | DTC01 et al., quote included in the account |
| Skill-description overlap arbitration | DTS01 trigger-overlap |
| Cross-mechanism precedence (memory vs skill vs subagent vs command) | DTP04 cross-layer-conflict |
| Cross-tool divergent reading of the same tree (Zed first-match vs merge) | DTP05 divergent-interpretation |
| Silent truncation priority (Codex 32 KiB halt; skill listing cap) | DTP06 unreachable-instruction |

If a finding's precedence account reads "no documented precedence" or "ordering is
unspecified", this table is why: the ambiguity is the ecosystem's, and the finding is asking
you to declare the intent the ecosystem won't.

## What discovery covers (and does not)

detangle scans the repository you point it at. Layers that live outside the repo —
enterprise/managed policy files, `~/.claude` user-global memory and rules, Cursor Team rules,
Copilot personal/organization instructions — are part of the *precedence model* (accounts
mention them where relevant) but are not discovered by a repo scan. Tests and CI can simulate
the user-global layer by setting `Config.user_dir` programmatically.
