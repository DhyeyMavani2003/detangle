"""Screen lane: a whole-config LLM sweep that NOMINATES conflict pairs.

The deterministic lane's recall ceiling is candidate formation: a conflict
whose phrasing defeats the lexicons never becomes a pair, so no downstream
judge ever sees it. The screen lane attacks exactly that. A strong model
reads EVERY extracted unit (including the weak, hedged sentences the
precision-first classifier rejects) with file/layer/activation metadata,
and nominates suspicious pairs — including the classes only whole-config
reasoning can see:

- **procedural/order conflicts** — step A-before-B vs B-before-A, and
  skill-orchestration order (an always-on file prescribing skill invocation
  order vs a skill body's own claims);
- **cross-layer conflicts** — the always-on CLAUDE.md/AGENTS.md vs the
  conditionally-loaded skill bodies that join the context when a skill
  fires;
- hedged/colloquial contradictions, numeric and format clashes phrased
  outside the deterministic vocabulary, semantic redundancy.

Nominations are NOT findings. Every nominated pair goes through the jury's
swap-validated adjudication protocol (both orderings, evidence validation,
verdict enums) — the screen buys recall, the jury keeps precision. This is
the research's group-screen -> pair-judge cascade.

DEEP multi-sweep nomination (``deep = true``): the single generic sweep
asks one call to spot every conflict class at once; recall improves when
each class gets its own focused pass, so deep mode re-reads the same units
once per class in ``FOCUSED_KINDS`` with a single-lens prompt and unions
the nominations. The existing pair dedup collapses cross-sweep duplicates,
and the jury still adjudicates everything — extra sweeps buy recall, never
precision loss.

Screen calls are cached by (backend, model, per-sweep prompt hash,
unit-set hash) — each sweep caches independently — so re-scans of an
unchanged config are free even in deep mode.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from ..activation import build_pair
from ..cache import VerdictCache
from ..config import Config
from ..detectors.base import AnalysisContext
from ..ir import CoActiveClass, InstructionUnit, UnitPair
from .backends import Backend, JuryError

SCREEN_SYSTEM_PROMPT = """You are auditing an AI agent's natural-language configuration for \
internal conflicts. You will receive the complete list of instruction units extracted from \
the config tree, each with an id, source file, layer (always-on memory vs conditionally \
loaded skill/rule bodies), and activation info.

Nominate every PAIR of units that a maintainer should look at, across these classes:
- direct contradictions (incompatible prescriptions for the same situation)
- conditional conflicts (jointly unsatisfiable when both activation conditions hold — \
remember: a skill's body joins the context TOGETHER WITH the always-on files when the \
skill fires, so main-file vs skill-body contradictions are real)
- procedure/order conflicts (one unit says do A before B, another says B before A; or an \
orchestrating file prescribes a skill-invocation order that a skill's own body contradicts)
- numeric/limit disagreements, output-format clashes, permit-vs-forbid
- semantic redundancy (the same rule stated twice in different words)
- soft tension (two goals that degrade each other, like brevity vs exhaustiveness)

Be RECALL-ORIENTED: nominate every genuinely suspicious pair — a second-stage judge \
verifies each nomination, so a plausible false nomination is cheap but a missed conflict \
is lost forever. Do NOT pad: unrelated units that merely share a topic are not suspicious, \
and most well-maintained configs contain few real conflicts.

Respond with ONLY a JSON array (no other text). Each element:
{"a": <unit id number>, "b": <unit id number>, "kind": "<contradiction|conditional|order|\
cross-layer|numeric|format|permit-forbid|redundant|tension>", "why": "<at most 25 words>"}
Return [] if nothing is suspicious."""

FOCUSED_KINDS = (
    "contradiction",
    "conditional",
    "order",
    "cross-layer",
    "numeric",
    "format",
    "permit-forbid",
    "redundant",
    "tension",
)

MAX_UNITS_PER_CALL = 150


def _sweep_prompts(deep: bool) -> list[tuple[str, str]]:
    """The sweep plan as (label, system prompt) tuples.

    Always the generic whole-taxonomy sweep; in deep mode, additionally one
    focused sweep per class in ``FOCUSED_KINDS`` — same units, same JSON
    output contract, but a single-lens reading (recall over speed)."""
    sweeps = [("generic", SCREEN_SYSTEM_PROMPT)]
    if not deep:
        return sweeps
    for kind in FOCUSED_KINDS:
        sweeps.append(
            (
                "focus:" + kind,
                SCREEN_SYSTEM_PROMPT
                + f"\n\nTHIS SWEEP IS FOCUSED: examine the units ONLY for the '{kind}' "
                "class described above. Read every unit with that single lens and "
                "nominate every pair that could plausibly belong to it; other classes "
                "are handled by other sweeps, so do not report them here.",
            )
        )
    return sweeps


def _screen_cache(cfg: Config) -> VerdictCache:
    """Nomination cache in its own file (``<cache-dir>/screen/verdicts.json``).

    The jury lane opens ``verdicts.json`` before this lane runs and saves it
    LAST, so sharing that file would let the jury's save clobber nominations
    the screen saved mid-run; a dedicated file keeps a re-scan of an
    unchanged config at zero screen calls."""
    base = Path(cfg.cache_dir or (cfg.root / ".detangle-cache"))
    return VerdictCache(base / "screen")


def _unit_line(i: int, u: InstructionUnit) -> str:
    layer = f"{u.file.mechanism}/{u.layer.value}"
    act = u.activation.mode.value
    extra = ""
    if u.activation.globs:
        extra = f" globs={list(u.activation.globs)}"
    elif u.activation.mode.value == "model":
        desc = " ".join(u.activation.description.split()[:14])
        extra = f' trigger="{desc}"'
    return f"[{i}] ({u.file.path}, {layer}, {act}{extra}) {json.dumps(u.text)}"


def _chunks(units: list[InstructionUnit]) -> list[list[tuple[int, InstructionUnit]]]:
    """Chunk large configs, repeating always-on units in every chunk so
    cross-layer pairs against the main files survive chunking."""
    indexed = list(enumerate(units))
    if len(indexed) <= MAX_UNITS_PER_CALL:
        return [indexed]
    always = [(i, u) for i, u in indexed if u.activation.mode.value == "always"]
    conditional = [(i, u) for i, u in indexed if u.activation.mode.value != "always"]
    room = max(MAX_UNITS_PER_CALL - len(always), 20)
    out = []
    for start in range(0, len(conditional), room):
        out.append(always + conditional[start : start + room])
    return out or [indexed]


def _parse_nominations(raw: str, n_units: int) -> list[tuple[int, int, str, str]]:
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    out: list[tuple[int, int, str, str]] = []
    if not isinstance(data, list):
        return out
    for item in data:
        if not isinstance(item, dict):
            continue
        try:
            a, b = int(item["a"]), int(item["b"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (0 <= a < n_units and 0 <= b < n_units) or a == b:
            continue
        out.append((a, b, str(item.get("kind", "")), str(item.get("why", ""))[:200]))
    return out


def run_screen_lane(cfg: Config, ctx: AnalysisContext, backend: Backend) -> list[UnitPair]:
    """Nominate pairs with the screen model; returns swap-ready UnitPairs.

    Runs one generic sweep, plus one focused sweep per conflict class when
    ``cfg.deep`` is set; nominations are unioned across sweeps and deduped.
    The caller (the jury lane) adjudicates them; the screen itself never
    emits findings.
    """
    units = ctx.units
    if len(units) < 2:
        return []
    cache = _screen_cache(cfg)
    # hash the exact listing lines the model sees — activation/layer metadata
    # included — so a trigger-description or glob edit misses the cache
    unit_hash = hashlib.sha256(
        "\x00".join(_unit_line(i, u) for i, u in enumerate(units)).encode()
    ).hexdigest()[:16]
    sweeps = _sweep_prompts(cfg.deep)
    chunks = _chunks(units)

    complete = True
    nominations: list[tuple[int, int, str, str]] = []
    for _label, prompt in sweeps:
        prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()[:12]
        for chunk in chunks:
            chunk_ids = hashlib.sha256(str([i for i, _ in chunk]).encode()).hexdigest()[:8]
            key = cache.key(f"{backend.ident}|screen", prompt_hash, f"{unit_hash}|{chunk_ids}")
            hit = cache.get(key)
            if hit is not None:
                nominations.extend(tuple(x) for x in hit)
                continue
            listing = "\n".join(_unit_line(i, u) for i, u in chunk)
            try:
                raw = backend.complete(prompt, listing + "\n\nNominate pairs. JSON only.")
            except JuryError as e:
                ctx.corpus.notes.append(f"screen lane: backend failure ({e}); sweep incomplete")
                complete = False
                continue
            found = _parse_nominations(raw, len(units))
            cache.put(key, [list(x) for x in found])
            nominations.extend(found)
    cache.save()

    seen: set[str] = set()
    pairs: list[UnitPair] = []
    for a_i, b_i, kind, _why in nominations:
        a, b = units[a_i], units[b_i]
        if a.uid == b.uid and a.span.start_line == b.span.start_line:
            continue
        pair = build_pair(a, b)
        if pair.co_active == CoActiveClass.MUTUALLY_EXCLUSIVE:
            continue
        pair_key = pair.key + f"|{a.span.start_line}|{b.span.start_line}"
        if pair_key in seen or ctx.is_claimed(pair):
            continue
        seen.add(pair_key)
        pair.block_keys = (f"screen:{kind or 'nominated'}",)
        pairs.append(pair)
    if complete:
        ctx.lanes_ran.add("screen")
    ctx.corpus.notes.append(
        f"screen lane: {len(nominations)} nomination(s) from {backend.ident} across "
        f"{len(sweeps)} sweep(s), {len(pairs)} pair(s) sent to the jury"
    )
    return pairs
