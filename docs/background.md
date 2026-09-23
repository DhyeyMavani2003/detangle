# Background

## Why lint agent instructions

An agent's configuration (`CLAUDE.md`, `AGENTS.md`, skills, rules, subagents) is a program
written in English, edited by many hands and executed by a model. Code gets linters, type
checkers and CI; agent instructions usually get none of that.

The vendors already describe the problem:

- Anthropic's docs: *"if two rules contradict each other, Claude may pick one arbitrarily."*
- OpenAI's GPT-5 guide: contradictory instructions cause the model to *"expend reasoning
  tokens searching for a way to reconcile the contradictions."*
- In July 2026, Anthropic found conflicting directives in Claude Code's own configuration
  ("leave documentation as appropriate" vs "DO NOT add comments") and removed over 80% of its
  system prompt with no measured loss.

The research this project was built from points the same way: models do not resolve
instruction conflicts reliably at runtime (the best open model scores about 48% on the IHEval
instruction-hierarchy benchmark, and which rule wins depends on position and model), config
files accrete conflicts as they grow (about 4.9 instructions added per commit that touches
them), and 91 of 100 real `AGENTS.md`/`CLAUDE.md` files sampled carried at least one config
smell. The cheap place to catch a conflict is lint time, not inference time.

## What makes the analysis different from a format linter

- **Co-activation aware.** Two instructions that can never be in context together cannot
  conflict. detangle models each ecosystem's loading rules (launch sets, glob-scoped rules,
  description-triggered skills, isolated subagent contexts) and drops impossible pairs
  before any judgment. See [ecosystems.md](ecosystems.md).
- **Precedence aware.** A conflict resolved by a declared hierarchy is not a conflict; one
  with no declared order is. detangle encodes each ecosystem's documented precedence,
  including the polarity flips (Claude Code skills are personal over project, but subagents
  are project over user), and phrases every finding accordingly.
- **Witness scenarios.** For conditional conflicts, the finding states the situation in which
  both instructions apply and cannot both be followed.
- **Deterministic core.** The default mode makes zero LLM calls and zero network calls and is
  reproducible byte for byte, so it is safe for CI and air-gapped repos. The optional lanes
  add semantic judgment on top ([lanes.md](lanes.md)).

## Roadmap

- **Formal lane:** clingo/ASP and Z3 encodings for the formalizable subset (numeric limits,
  scoped permissions, ordering) with unsat-core witnesses. The reserved codes DTC06 and DTC07
  wait on it.
- **New TypeSafe questions** aimed at known gaps: skill-description overlap, drift between
  near-duplicates, whole-procedure ordering, and a witness check on emitted findings
  ([experiments.md](experiments.md)).
- **A fresh, sealed benchmark** built from real public agent configs, so lane comparisons no
  longer rest on a set the TypeSafe lane was tuned on ([benchmark.md](benchmark.md)).
- **Precedence manifest:** declare the intended resolution order (`overrides:` front
  matter) and have detangle check against it instead of flagging ambiguity.
- **PR semantic diff:** "this change makes rule R newly shadowed" or "widens what the agent
  may do".
- **PyPI release**, so installation is `pip install detangle`.
