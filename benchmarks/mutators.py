"""Conflict-injection operators for the seeded-conflict benchmark.

Mutation-testing for the linter (digest-10): each operator takes a clean base
tree (``{relpath: text}``) and a deterministic seed, injects exactly ONE
labeled defect, and returns ``(mutated_tree, injection_record)`` where::

    injection_record = {
        "operator": str,
        "expected_codes": [str, ...],   # codes that count as a detection
        "files": [str, ...],            # files touched/created by the injection
        "sites": [{"file": str, "text": str}, ...],  # the two conflicting
                                        # texts (for pair-granular scoring)
        "description": str,
        "control": bool,                # equivalent-mutant control?
    }

Nine conflict operators seed a real conflict; two equivalent-mutant controls
(``paraphrase``, ``benign_specialization``) change the tree without changing
its meaning — any conflict-class finding on a control run is a false positive.

Operators never modify the input dict; all randomness flows through
``random.Random(seed)`` so a (tree, operator, seed) triple is reproducible.

HONESTY CAVEAT (measured, not hypothetical): these operators select and phrase
injections through detangle's OWN parser and lexicons — ``_line_qualifies``
imports ``extract_frame``/``detect_modality``, the format templates match the
detector's ``FORMAT_EXCLUSIVE_RE``, and ``_BOUND_RE``'s comparators are a
subset of ``COMPARATOR_PHRASES``. The detection rate they yield is therefore
*in-distribution self-consistency*, not generalization: it answers "does the
pipeline catch conflicts phrased in its own vocabulary?" and nothing more.
Recall on novel, realistic phrasings is measured separately by the
hand-authored :mod:`benchmarks.holdout` set, and the eval report labels the
two numbers accordingly.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from random import Random

from detangle.extract import detect_modality, extract_frame, split_condition
from detangle.ir import Modality
from detangle.lexicons import IMPERATIVE_VERBS

Tree = dict[str, str]
Record = dict[str, object]
Mutator = Callable[[Tree, int], tuple[Tree, Record]]

#: The conflict-class codes controls must never trigger (DTC01–05, DTP01–04).
CONFLICT_CODES = frozenset(
    {"DTC01", "DTC02", "DTC03", "DTC04", "DTC05", "DTP01", "DTP02", "DTP03", "DTP04"}
)


class MutationError(RuntimeError):
    """The operator's preconditions are not met by this tree."""


# ---------------------------------------------------------------------------
# Tree introspection helpers
# ---------------------------------------------------------------------------

_ROOT_MEMORY = (
    "CLAUDE.md",
    ".claude/CLAUDE.md",
    "CLAUDE.local.md",
    "AGENTS.md",
    "AGENT.md",
    ".cursorrules",
    ".github/copilot-instructions.md",
)

_FM_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _frontmatter(text: str) -> str:
    m = _FM_RE.match(text)
    return m.group(1) if m else ""


def _body(text: str) -> str:
    m = _FM_RE.match(text)
    return text[m.end() :] if m else text


def _fm_has(text: str, *keys: str) -> bool:
    fm = _frontmatter(text)
    return any(re.search(rf"^{re.escape(k)}\s*:", fm, re.MULTILINE) for k in keys)


def _fm_true(text: str, key: str) -> bool:
    return bool(re.search(rf"^{re.escape(key)}\s*:\s*true\b", _frontmatter(text), re.MULTILINE))


def activation_of(path: str, text: str) -> str:
    """Coarse activation class of a config file: always | path | model | other."""
    if path in _ROOT_MEMORY:
        return "always"
    if path.endswith(("/AGENTS.md", "/AGENT.md")):
        return "path"  # subdirectory AGENTS.md scopes to its subtree
    if path.startswith(".claude/rules/") and path.endswith(".md"):
        return "path" if _fm_has(text, "paths", "globs") else "always"
    if path.startswith(".claude/skills/") and path.endswith("/SKILL.md"):
        return "model"
    if path.startswith(".cursor/rules/") and path.endswith(".mdc"):
        if _fm_true(text, "alwaysApply") or _fm_true(text, "always_apply"):
            return "always"
        if _fm_has(text, "globs"):
            return "path"
        if _fm_has(text, "description"):
            return "model"
        return "other"
    if path.startswith(".github/instructions/") and path.endswith(".instructions.md"):
        return "path" if _fm_has(text, "applyTo", "applyto") else "always"
    return "other"


def _activations(tree: Tree) -> dict[str, str]:
    return {p: activation_of(p, t) for p, t in sorted(tree.items())}


def _files_of(tree: Tree, kind: str, exclude: str = "") -> list[str]:
    return [p for p, a in _activations(tree).items() if a == kind and p != exclude]


def _config_files(tree: Tree) -> list[str]:
    return [p for p, a in _activations(tree).items() if a in {"always", "path", "model"}]


def _source_files(tree: Tree) -> list[str]:
    """Non-config files with a directory and an extension (for glob targeting)."""
    acts = _activations(tree)
    out = []
    for p in sorted(tree):
        if acts.get(p) != "other" or "/" not in p:
            continue
        base = p.rsplit("/", 1)[1]
        if "." in base and not base.endswith(".md"):
            out.append(p)
    return out


def _pick(rng: Random, seq: list) -> object:
    if not seq:
        raise MutationError("no candidates in this tree")
    return seq[rng.randrange(len(seq))]


def _appended(tree: Tree, path: str, block: str) -> Tree:
    new = dict(tree)
    text = new.get(path, "")
    if text and not text.endswith("\n"):
        text += "\n"
    new[path] = text + block if block.endswith("\n") else text + block + "\n"
    return new


# ---------------------------------------------------------------------------
# Instruction-line candidates
# ---------------------------------------------------------------------------

_FORMAT_WORDS_RE = re.compile(r"\b(json|yaml|xml|markdown|html|prose|csv)\b", re.IGNORECASE)


def _line_qualifies(line: str) -> bool:
    """A ``- Always/Never <verb> …`` bullet the deterministic frames fully parse.

    Requires a known imperative verb and a non-empty extracted object so that
    a negated copy provably registers as a modality disagreement; excludes
    digits (DTC03 territory) and format tokens (DTC04 territory).
    """
    if not re.match(r"^- (Always|Never)\s+\S", line):
        return False
    text = line[2:].strip()
    if re.search(r"\d", text) or _FORMAT_WORDS_RE.search(text):
        return False
    body, _cond = split_condition(text)
    frame = extract_frame(body, detect_modality(text))
    return bool(
        frame.action in IMPERATIVE_VERBS
        and frame.obj
        and frame.modality in (Modality.OBLIGE, Modality.FORBID)
    )


def _obligation_lines(tree: Tree, paths: list[str]) -> list[tuple[str, str]]:
    out = []
    for path in paths:
        for ln in _body(tree[path]).split("\n"):
            ln = ln.rstrip()
            if _line_qualifies(ln):
                out.append((path, ln))
    return out


def _instruction_lines(tree: Tree, paths: list[str]) -> list[tuple[str, str]]:
    """Bullet lines the extractor treats as instructions (for verbatim copies)."""
    out = []
    for path in paths:
        for ln in _body(tree[path]).split("\n"):
            ln = ln.rstrip()
            m = re.match(r"^- ([A-Za-z][\w-]*)\b", ln)
            if not m:
                continue
            first = m.group(1).lower()
            if first in IMPERATIVE_VERBS or first in {"always", "never", "prefer", "avoid"}:
                out.append((path, ln))
    return out


def _flip_line(line: str) -> str:
    if line.startswith("- Always"):
        return line.replace("- Always", "- Never", 1)
    return line.replace("- Never", "- Always", 1)


def _inject_target(tree: Tree, rng: Random, src: str) -> str:
    """A co-active file to inject into: another always-on file, else a
    path-scoped file, else the source file itself."""
    always = _files_of(tree, "always", exclude=src)
    if always:
        return str(_pick(rng, always))
    pathy = _files_of(tree, "path", exclude=src)
    if pathy:
        return str(_pick(rng, pathy))
    return src


def _record(
    operator: str,
    expected: list[str],
    files: list[str],
    description: str,
    control: bool = False,
    sites: list[tuple[str, str]] | None = None,
) -> Record:
    """Build an injection record.

    ``sites`` names the two halves of the seeded conflict as ``(file, text)``
    pairs (text without the ``- `` bullet prefix). When present, ``run_eval``
    scores the run pair-granularly: a finding only counts if its evidence
    touches BOTH sites. Omit it (controls, or operators where the pair is not
    well-defined) to fall back to file-granular scoring.
    """
    rec: Record = {
        "operator": operator,
        "expected_codes": list(expected),
        "files": sorted(set(files)),
        "description": description,
        "control": control,
    }
    if sites is not None:
        rec["sites"] = [{"file": f, "text": t} for f, t in sites]
    return rec


def _bullet_text(line: str) -> str:
    """The instruction text of a ``- ...`` bullet line."""
    return line[2:].strip() if line.startswith("- ") else line.strip()


# ---------------------------------------------------------------------------
# 1. deontic_flip — copy an obligation into a co-active file, negated
# ---------------------------------------------------------------------------


def deontic_flip(tree: Tree, seed: int) -> tuple[Tree, Record]:
    rng = Random(seed)
    src, line = _pick(rng, _obligation_lines(tree, _files_of(tree, "always")))
    flipped = _flip_line(line)
    always = _files_of(tree, "always", exclude=src)
    target = str(_pick(rng, always)) if always else src
    mutated = _appended(tree, target, flipped)
    return mutated, _record(
        "deontic_flip",
        ["DTC01", "DTP04"],
        [src, target],
        f"negated copy of {src!r} obligation injected into {target!r}: {flipped[2:]!r}",
        sites=[(src, _bullet_text(line)), (target, _bullet_text(flipped))],
    )


# ---------------------------------------------------------------------------
# 2. parameter_clash — inject a conflicting numeric constraint
# ---------------------------------------------------------------------------

_BOUND_RE = re.compile(
    r"\b(?P<cmp>at most|at least|no more than|under|within|fewer than|less than|more than|over)"
    r"\s+(?P<num>\d+)\s+"
    r"(?P<unit>times|lines|words|seconds|minutes|hours|retries|attempts|characters|files|items)\b",
    re.IGNORECASE,
)
_UPPER_BOUNDS = {"at most", "no more than", "under", "within", "fewer than", "less than"}


def _bounded_lines(tree: Tree, paths: list[str]) -> list[tuple[str, str, re.Match]]:
    out = []
    for path in paths:
        for ln in _body(tree[path]).split("\n"):
            ln = ln.rstrip()
            if not ln.startswith("- "):
                continue
            m = _BOUND_RE.search(ln)
            if m:
                out.append((path, ln, m))
    return out


def parameter_clash(tree: Tree, seed: int) -> tuple[Tree, Record]:
    rng = Random(seed)
    src, line, m = _pick(rng, _bounded_lines(tree, _files_of(tree, "always")))
    n = int(m.group("num"))
    if m.group("cmp").lower() in _UPPER_BOUNDS:
        clash_n = 2 * n + rng.randint(1, 9)  # above the upper bound
    else:
        clash_n = rng.randint(0, max(0, (n - 1) // 2))  # below the lower bound
    clashing = line[: m.start()] + f"exactly {clash_n} {m.group('unit')}" + line[m.end() :]
    target = _inject_target(tree, rng, src)
    mutated = _appended(tree, target, clashing)
    return mutated, _record(
        "parameter_clash",
        ["DTC03"],
        [src, target],
        f"{line[2:]!r} in {src!r} vs injected {clashing[2:]!r} in {target!r}",
        sites=[(src, _bullet_text(line)), (target, _bullet_text(clashing))],
    )


# ---------------------------------------------------------------------------
# 3. scope_overlap_clash — intersecting globs, opposite prescriptions
# ---------------------------------------------------------------------------

_SCOPE_PRESCRIPTIONS = (
    (
        "Always write docstrings for public functions.",
        "Never write docstrings for public functions.",
    ),
    ("Always run the formatter before committing.", "Never run the formatter before committing."),
    ("Always use double quotes for strings.", "Never use double quotes for strings."),
)


def _scoped_rule_files(tree: Tree, glob_a: str, glob_b: str, pos: str, neg: str) -> dict[str, str]:
    """Two new path-scoped rule files in whichever surface the tree speaks."""
    keys = set(tree)
    if "CLAUDE.md" in keys or any(k.startswith(".claude/") for k in keys):
        return {
            ".claude/rules/injected-scope-a.md": f'---\npaths: "{glob_a}"\n---\n- {pos}\n',
            ".claude/rules/injected-scope-b.md": f'---\npaths: "{glob_b}"\n---\n- {neg}\n',
        }
    if any(k.startswith(".cursor/") for k in keys):
        return {
            ".cursor/rules/injected-scope-a.mdc": f'---\nglobs: "{glob_a}"\n---\n- {pos}\n',
            ".cursor/rules/injected-scope-b.mdc": f'---\nglobs: "{glob_b}"\n---\n- {neg}\n',
        }
    return {
        ".github/instructions/injected-scope-a.instructions.md": (
            f'---\napplyTo: "{glob_a}"\n---\n- {pos}\n'
        ),
        ".github/instructions/injected-scope-b.instructions.md": (
            f'---\napplyTo: "{glob_b}"\n---\n- {neg}\n'
        ),
    }


def scope_overlap_clash(tree: Tree, seed: int) -> tuple[Tree, Record]:
    rng = Random(seed)
    src = str(_pick(rng, _source_files(tree)))
    top = src.split("/", 1)[0]
    ext = src.rsplit(".", 1)[1]
    glob_a, glob_b = f"{top}/**", f"**/*.{ext}"  # partial overlap: neither subsumes
    pos, neg = _pick(rng, list(_SCOPE_PRESCRIPTIONS))
    new_files = _scoped_rule_files(tree, glob_a, glob_b, pos, neg)
    mutated = dict(tree)
    mutated.update(new_files)
    paths = list(new_files)
    return mutated, _record(
        "scope_overlap_clash",
        ["DTP02", "DTC02"],
        paths,
        f"opposite prescriptions on intersecting scopes {glob_a!r} vs {glob_b!r}: {pos!r}",
        sites=[(paths[0], pos), (paths[1], neg)],
    )


# ---------------------------------------------------------------------------
# 4. conditional_contradiction — same action, contradicting guards
# ---------------------------------------------------------------------------

_CONDITIONAL_PAIRS = (
    (
        "- When preparing a release, always tag the final commit.",
        "- When working on an experiment branch, never tag the final commit.",
    ),
    (
        "- When touching payment code, always add an audit log entry.",
        "- When running in a sandbox, never add an audit log entry.",
    ),
)


def conditional_contradiction(tree: Tree, seed: int) -> tuple[Tree, Record]:
    rng = Random(seed)
    target = str(_pick(rng, _files_of(tree, "always")))
    pos, neg = _pick(rng, list(_CONDITIONAL_PAIRS))
    mutated = _appended(tree, target, pos + "\n" + neg)
    return mutated, _record(
        "conditional_contradiction",
        ["DTC02", "DTC01"],
        [target],
        f"guarded contradiction injected into {target!r}: {pos[2:]!r} vs {neg[2:]!r}",
        sites=[(target, _bullet_text(pos)), (target, _bullet_text(neg))],
    )


# ---------------------------------------------------------------------------
# 5. terminology_drift — redefine a term differently elsewhere
# ---------------------------------------------------------------------------

_TERM_DEFS = (
    (
        "golden path",
        '- "Golden path" means the fully supported checkout flow from cart to confirmation.',
        '- "Golden path" means the scripted demo we walk enterprise customers through.',
    ),
    (
        "hotfix",
        '- "Hotfix" means a patch branched from the most recent release tag.',
        '- "Hotfix" means any change that skips the normal review queue.',
    ),
)


def terminology_drift(tree: Tree, seed: int) -> tuple[Tree, Record]:
    rng = Random(seed)
    files = _config_files(tree)
    if len(files) < 2:
        raise MutationError("need two config files to seed terminology drift")
    file_a = str(_pick(rng, files))
    file_b = str(_pick(rng, [p for p in files if p != file_a]))
    term, def_a, def_b = _pick(rng, list(_TERM_DEFS))
    mutated = _appended(_appended(tree, file_a, def_a), file_b, def_b)
    return mutated, _record(
        "terminology_drift",
        ["DTR03"],
        [file_a, file_b],
        f"term {term!r} defined differently in {file_a!r} and {file_b!r}",
        sites=[(file_a, _bullet_text(def_a)), (file_b, _bullet_text(def_b))],
    )


# ---------------------------------------------------------------------------
# 6. cross_layer_clash — skill/model-layer body contradicts the memory layer
# ---------------------------------------------------------------------------

_INJECTED_SKILL_PATH = ".claude/skills/injected-conventions/SKILL.md"
_INJECTED_SKILL_HEADER = """\
---
name: injected-conventions
description: Use when auditing third-party license obligations before a vendor review.
---
# Conventions

"""


def cross_layer_clash(tree: Tree, seed: int) -> tuple[Tree, Record]:
    rng = Random(seed)
    src, line = _pick(rng, _obligation_lines(tree, _files_of(tree, "always")))
    flipped = _flip_line(line)
    model_files = _files_of(tree, "model")
    mutated = dict(tree)
    if model_files:
        target = str(_pick(rng, model_files))
        mutated = _appended(mutated, target, flipped)
    else:
        target = _INJECTED_SKILL_PATH
        mutated[target] = _INJECTED_SKILL_HEADER + flipped + "\n"
    return mutated, _record(
        "cross_layer_clash",
        ["DTP04", "DTC01"],
        [src, target],
        f"model-triggered body {target!r} contradicts {src!r}: {flipped[2:]!r}",
        sites=[(src, _bullet_text(line)), (target, _bullet_text(flipped))],
    )


# ---------------------------------------------------------------------------
# 7. duplicate_injection — verbatim copy in another co-active file
# ---------------------------------------------------------------------------


def duplicate_injection(tree: Tree, seed: int) -> tuple[Tree, Record]:
    rng = Random(seed)
    src, line = _pick(rng, _instruction_lines(tree, _files_of(tree, "always")))
    # a same-file verbatim copy collapses to one content-addressed unit, so the
    # copy must land in a *different* co-active file
    always = _files_of(tree, "always", exclude=src)
    pathy = _files_of(tree, "path", exclude=src)
    target = str(_pick(rng, always or pathy))
    mutated = _appended(tree, target, line)
    return mutated, _record(
        "duplicate_injection",
        ["DTR01", "DTR02"],
        [src, target],
        f"verbatim copy of {line[2:]!r} from {src!r} into {target!r}",
        sites=[(src, _bullet_text(line)), (target, _bullet_text(line))],
    )


# ---------------------------------------------------------------------------
# 8. format_clash — mutually exclusive output-format constraints
# ---------------------------------------------------------------------------

_FORMAT_PAIRS = (
    (
        "- Always respond in JSON only.",
        "- Format every response as Markdown, nothing but Markdown.",
    ),
    (
        "- Always respond in YAML only.",
        "- Write every answer strictly as JSON.",
    ),
)


def format_clash(tree: Tree, seed: int) -> tuple[Tree, Record]:
    rng = Random(seed)
    always = _files_of(tree, "always")
    file_a = str(_pick(rng, always))
    others = [p for p in always if p != file_a]
    file_b = str(_pick(rng, others)) if others else file_a
    line_a, line_b = _pick(rng, list(_FORMAT_PAIRS))
    mutated = _appended(_appended(tree, file_a, line_a), file_b, line_b)
    return mutated, _record(
        "format_clash",
        ["DTC04"],
        [file_a, file_b],
        f"exclusive format constraints injected: {line_a[2:]!r} vs {line_b[2:]!r}",
        sites=[(file_a, _bullet_text(line_a)), (file_b, _bullet_text(line_b))],
    )


# ---------------------------------------------------------------------------
# 9. trigger_overlap — near-duplicate model-trigger description
# ---------------------------------------------------------------------------

_DESC_SWAPS = (
    ("asks", "wants"),
    ("changing", "updating"),
    ("reports", "mentions"),
    ("draft", "write"),
)

_FALLBACK_DESC = (
    "Use when the user asks to prepare a release announcement or draft the launch notes."
)


def _tweak_description(desc: str, rng: Random) -> str:
    words = desc.split()
    swaps = [(i, b) for i, w in enumerate(words) for a, b in _DESC_SWAPS if w.lower() == a]
    if swaps:
        i, b = swaps[rng.randrange(len(swaps))]
        words[i] = b
    return " ".join(words)


def _description_of(text: str) -> str:
    m = re.search(r"^description:\s*(.+)$", _frontmatter(text), re.MULTILINE)
    return m.group(1).strip().strip("\"'") if m else ""


def _twin_file(path_hint: str, name: str, desc: str, body: str) -> tuple[str, str]:
    if path_hint.endswith(".mdc"):
        return (f".cursor/rules/{name}.mdc", f"---\ndescription: {desc}\n---\n{body}\n")
    return (
        f".claude/skills/{name}/SKILL.md",
        f"---\nname: {name}\ndescription: {desc}\n---\n{body}\n",
    )


def trigger_overlap(tree: Tree, seed: int) -> tuple[Tree, Record]:
    rng = Random(seed)
    model_files = [p for p in _files_of(tree, "model") if _description_of(tree[p])]
    mutated = dict(tree)
    if model_files:
        src = str(_pick(rng, model_files))
        src_desc = _description_of(tree[src])
        desc = _tweak_description(src_desc, rng)
        twin_path, twin_text = _twin_file(
            src, "injected-twin", desc, "Follow the sibling rule for this task."
        )
        mutated[twin_path] = twin_text
        files = [src, twin_path]
        sites = [(src, src_desc), (twin_path, desc)]
        detail = f"near-duplicate of {src!r} trigger description as {twin_path!r}"
    else:
        # no model-triggered surface: seed a colliding pair of skills
        desc_b = _tweak_description(_FALLBACK_DESC, rng)
        path_a, text_a = _twin_file(
            "SKILL.md", "injected-twin-a", _FALLBACK_DESC, "Collect the merged changes first."
        )
        path_b, text_b = _twin_file(
            "SKILL.md",
            "injected-twin-b",
            desc_b,
            "Summarize what shipped, then post it.",
        )
        mutated[path_a] = text_a
        mutated[path_b] = text_b
        files = [path_a, path_b]
        sites = [(path_a, _FALLBACK_DESC), (path_b, desc_b)]
        detail = f"two new skills with colliding trigger descriptions: {path_a!r}, {path_b!r}"
    return mutated, _record("trigger_overlap", ["DTS01"], files, detail, sites=sites)


# ---------------------------------------------------------------------------
# Equivalent-mutant controls
# ---------------------------------------------------------------------------

_ALWAYS_PARAPHRASES = ("- Make sure to ", "- Be sure to ", "- You must ")
_NEVER_PARAPHRASES = ("- Do not ", "- You must not ")


def paraphrase(tree: Tree, seed: int) -> tuple[Tree, Record]:
    """Control: the same rule reworded in another co-active file.

    May legitimately fire DTR01/DTR02 (it *is* a redundancy); must never fire
    a conflict-class code (DTC01–05, DTP01–04).
    """
    rng = Random(seed)
    src, line = _pick(rng, _obligation_lines(tree, _files_of(tree, "always")))
    if line.startswith("- Always "):
        prefix = str(_pick(rng, list(_ALWAYS_PARAPHRASES)))
        rest = line[len("- Always ") :]
    else:
        prefix = str(_pick(rng, list(_NEVER_PARAPHRASES)))
        rest = line[len("- Never ") :]
    reworded = prefix + rest
    target = _inject_target(tree, rng, src)
    mutated = _appended(tree, target, reworded)
    return mutated, _record(
        "paraphrase",
        ["DTR01", "DTR02"],
        [src, target],
        f"meaning-preserving rewording of {src!r} line in {target!r}: {reworded[2:]!r}",
        control=True,
    )


def benign_specialization(tree: Tree, seed: int) -> tuple[Tree, Record]:
    """Control: the same prescription restated under a narrower scope.

    May fire DTR01 (redundancy); must never fire a conflict-class code.
    """
    rng = Random(seed)
    src, line = _pick(rng, _obligation_lines(tree, _files_of(tree, "always")))
    pathy = _files_of(tree, "path", exclude=src)
    if pathy:
        target = str(_pick(rng, pathy))
        mutated = _appended(tree, target, line)
    else:
        glob = f"{str(_pick(rng, _source_files(tree))).split('/', 1)[0]}/**"
        target = ".claude/rules/injected-specialization.md"
        mutated = dict(tree)
        mutated[target] = f'---\npaths: "{glob}"\n---\n{line}\n'
    return mutated, _record(
        "benign_specialization",
        ["DTR01"],
        [src, target],
        f"same prescription from {src!r} restated in narrower-scoped {target!r}",
        control=True,
    )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

#: The nine conflict-injection operators, in digest-10 order (where implemented).
OPERATORS: dict[str, Mutator] = {
    "deontic_flip": deontic_flip,
    "parameter_clash": parameter_clash,
    "scope_overlap_clash": scope_overlap_clash,
    "conditional_contradiction": conditional_contradiction,
    "terminology_drift": terminology_drift,
    "cross_layer_clash": cross_layer_clash,
    "duplicate_injection": duplicate_injection,
    "format_clash": format_clash,
    "trigger_overlap": trigger_overlap,
}

#: Equivalent-mutant controls for the false-positive rate.
CONTROLS: dict[str, Mutator] = {
    "paraphrase": paraphrase,
    "benign_specialization": benign_specialization,
}

ALL_MUTATORS: dict[str, Mutator] = {**OPERATORS, **CONTROLS}
