"""TypeSafe lane: calibrated pairwise conflict judgments via typed questions.

Where the jury asks a generative model to *write* a verdict and we parse it,
this lane asks a System One decision model (TypeSafe's Jev) a typed Choice
question per instruction pair and reads back a probability distribution over
relationship classes. Two properties make it a different kind of lane:

- **Batching.** Every question in a request sees the same ``state`` and is
  evaluated in parallel, so a batch of ``pairs_per_call`` pairs (default 20),
  each asked in both orderings, rides in one call — a 49-tree holdout
  adjudicates in about 20 seconds, not hours.
- **Calibration.** Emission is thresholded on the returned probabilities, and
  on the holdout the lane reached the deep opus+opus cascade's recall with
  zero false positives at every threshold tried (docs/lanes.md).

Each pair is asked in BOTH orderings inside the same request (the jury's
order-swap guard, at no extra round trip); the conflict mass used for
emission is the minimum across the two, and the class is the argmax of the
two orderings' mean distribution (single-ordering argmaxes flip on near
ties; the mean does not). A structural overlay then mirrors the
deterministic router: conditionally-loaded layer vs another layer is a
cross-layer collision (DTP04), overlapping path-scoped rules a precedence
ambiguity (DTP02).

Judging runs in two passes. The batched pass shares one state across
``pairs_per_call`` pairs; because judgment measurably degrades as the shared
state grows (docs/lanes.md), every pair whose batched conflict mass reaches
the uncertain band is then re-asked ALONE — a two-unit state, the same two
questions. The solo verdict decides whether the pair is cleared or stays in
the band (batched noise clears, so the jury receives half the pairs), but a
pair FIRES only when both passes see the conflict: a promotion the batched
pass did not support is handed to the jury instead of emitted (precision
first — measured on the demo agent, such promotions were mostly pairs a
human had already rejected). Emission needs every reading to see the
conflict (the minimum mass crosses ``tau``); clearing needs both orderings
to agree there is none (the maximum mass stays below ``uncertain_low``).

Composability: pairs judged confidently are claimed; pairs whose conflict
mass lands in the uncertain band are handed to the jury lane (when enabled)
through the context's jury queue, so ``--typesafe --jury`` means "TypeSafe
decides what it can; a generative juror handles the rest".

Findings carry ``lanes: ["typesafe"]``. Verdicts are cached per pair by
(linter version, model, prompt hash, pair key), so re-scans of unchanged
configs make zero calls.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import time
import urllib.error
import urllib.request

from ..activation import build_pair, scope_relation
from ..config import Config
from ..detectors.base import AnalysisContext
from ..findings import Finding, pair_evidence
from ..ir import ActivationMode, CoActiveClass, InstructionUnit, Layer, UnitPair
from ..taxonomy import Severity
from .backends import JuryError
from .cachekey import make_cache

DEFAULT_ENDPOINT = "https://api.typesafe.ai/v1/systemone"

# Relationship vocabulary: one atomic Choice per pair. Keys are the API
# options; values are the rubric the model sees. Order-independent.
RELATION_CRITERIA = {
    "contradictory": (
        "Both apply to the same situation unconditionally and require incompatible things "
        "(do X vs never do X; use tool A vs use tool B for the same job; ask first vs act "
        "immediately). Not for disagreements about a number, an output format, the order "
        "of steps, or a permission — those have their own options."
    ),
    "conditional_conflict": (
        "Compatible in general, but they clash whenever a specific named condition holds "
        "(during a code freeze, during an incident, on the release branch, for files in "
        "both scopes): one side's guard carves out a situation in which the other cannot "
        "be followed."
    ),
    "numeric_limit_conflict": (
        "Both put a number, count, size or duration on the same knob and the two bounds "
        "cannot both hold (cap at 88 vs wrap past 120; at least 300 words vs no more than "
        "150; five minutes vs 90 seconds for the same timeout). Use this, not "
        "contradictory, whenever the disagreement is about a numeric value."
    ),
    "format_conflict": (
        "Both dictate the shape or serialization of the same output and the shapes are "
        "exclusive (plain prose vs bulleted list; bare JSON vs Markdown report; with vs "
        "without headings). Not for wording, tone, tense or content — only the format."
    ),
    "permit_vs_forbid": (
        "One side explicitly allows or invites an action (may, it's fine to, feel free, "
        "you're welcome to, without asking) and the other forbids that same action or "
        "requires approval for it, on the same object and scope. Not for two obligations "
        "that clash (that is contradictory)."
    ),
    "order_conflict": (
        "Both prescribe the order of the same two steps and disagree (lint then test vs "
        "test then lint; changelog before version bump vs bump first). Not for steps of "
        "different pipelines or before/after steps that chain compatibly."
    ),
    "goal_tension": (
        "Two soft preferences about degree or style (how brief, how thorough, how much "
        "to change) that can both be literally obeyed but pull against each other, so "
        "doing more of one means less of the other. Not for a specific action one side "
        "requires and the other rules out."
    ),
    "redundant": (
        "The same prescription stated twice in different words (they entail each other); "
        "no disagreement at all."
    ),
    "distinct": (
        "Compatible: different subjects, objects or scopes; one refines or narrows the "
        "other; or an explicit exception to it. Both can always be followed together."
    ),
}
CONFLICT_CLASSES = (
    "contradictory",
    "conditional_conflict",
    "numeric_limit_conflict",
    "format_conflict",
    "permit_vs_forbid",
    "order_conflict",
    "goal_tension",
)
_CODE_FOR = {
    "contradictory": "DTC01",
    "conditional_conflict": "DTC02",
    "numeric_limit_conflict": "DTC03",
    "format_conflict": "DTC04",
    "permit_vs_forbid": "DTC05",
    "order_conflict": "DTC02",  # a process conflict: the taxonomy's conditional class
    "goal_tension": "DTC08",
}
# classes whose code stays put under the structural overlay (the deterministic
# router likewise routes numeric clashes and soft tension before layer tests)
_OVERLAY_EXEMPT = {"numeric_limit_conflict", "goal_tension"}
# layers that join the context conditionally: a clash between one of these and
# any other layer is a cross-layer collision in the router's vocabulary
_CONDITIONAL_LAYERS = {
    Layer.SKILL,
    Layer.SUBAGENT,
    Layer.TOOL_DESC,
    Layer.PLUGIN,
    Layer.MCP_INSTRUCTIONS,
}

_COACT_NOTE = (
    "Each unit's `activation` field says when it is loaded: `always` units are in every "
    "session; `path` units load when the agent touches a file matching `activation_globs`; "
    "`model` units load when their `trigger` applies. `co_activation` below is the "
    "situation in which both are loaded together, and `precedence` says whether the tool "
    "resolves a disagreement between them. Judge the pair for that co-loaded situation; "
    "being loaded together is not by itself a clash."
)

_PROMPT_VERSION = "relation-v4-coact"
_PROMPT_HASH = hashlib.sha256(
    (_PROMPT_VERSION + _COACT_NOTE + json.dumps(RELATION_CRITERIA, sort_keys=True)).encode()
).hexdigest()[:12]

# request sizing: the API budget is ~32k tokens shared by state + questions
# (chars/2 is a pessimistic token estimate). Pairs per call is a CONFIG knob
# because judgment quality measurably degrades as the shared state grows —
# see docs/lanes.md — so small batches buy recall.
_BUDGET_TOKENS = 14_000
# HTTP statuses worth a retry with backoff; anything else ends the lane for the run
_TRANSIENT_HTTP = {429, 500, 502, 503, 504, 529}


class TypeSafeClient:
    """Minimal stdlib client for the System One evaluation endpoint."""

    def __init__(
        self,
        model: str = "jev-latest",
        api_key_env: str = "TYPESAFE_API_KEY",
        endpoint: str = DEFAULT_ENDPOINT,
        timeout: int = 120,
    ):
        self.api_key = os.environ.get(api_key_env, "")
        if not self.api_key:
            raise JuryError(f"typesafe lane requires {api_key_env} in the environment")
        self.model = model
        self.endpoint = endpoint
        self.timeout = timeout
        self.calls = 0

    @property
    def ident(self) -> str:
        return f"typesafe:{self.model}"

    def evaluate(self, state: object, questions: dict) -> dict:
        body = json.dumps(
            {"state": state, "model": self.model, "questions": questions},
            ensure_ascii=False,
        ).encode("utf-8")
        req = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        delay = 2.0
        for attempt in range(6):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                self.calls += 1
                answers = payload.get("answers") if isinstance(payload, dict) else None
                if not isinstance(answers, dict):
                    raise JuryError(f"typesafe: unexpected response shape: {str(payload)[:200]}")
                return answers
            except urllib.error.HTTPError as e:
                # the body is quoted in a scan note: never let it carry the key
                detail = e.read().decode("utf-8", "replace")[:300].replace(self.api_key, "***")
                if e.code in _TRANSIENT_HTTP and attempt < 5:
                    time.sleep(delay)
                    delay = min(delay * 2, 30)
                    continue
                raise JuryError(f"typesafe HTTP {e.code}: {detail}") from e
            except (
                urllib.error.URLError,
                OSError,  # connection reset / refused / timed out
                http.client.HTTPException,  # dropped connection, bad status line, short read
                json.JSONDecodeError,
            ) as e:
                if attempt < 5:
                    time.sleep(delay)
                    delay = min(delay * 2, 30)
                    continue
                raise JuryError(f"typesafe call failed: {e!r}") from e
        raise JuryError("typesafe: retries exhausted")  # pragma: no cover


def _unit_record(u: InstructionUnit) -> dict:
    rec = {
        "file": u.file.path,
        "layer": f"{u.file.mechanism}/{u.layer.value}",
        "activation": u.activation.mode.value,
        "text": u.text,
    }
    if u.activation.globs:
        rec["activation_globs"] = list(u.activation.globs)
    elif u.activation.mode.value == "model" and u.activation.description:
        rec["trigger"] = " ".join(u.activation.description.split()[:20])
    return rec


def _question(a_id: str, b_id: str, pair: UnitPair) -> dict:
    """One relation question. The instructions carry detangle's own account of
    WHEN the two units are loaded together and whether the tool resolves a
    disagreement between them — the judgment is for that co-loaded situation,
    which is what lets the model see a clash between two path-scoped rules
    whose globs intersect."""
    return {
        "type": "choice",
        "instructions": {
            "question": (
                f"Classify the relationship between `units.{a_id}` and `units.{b_id}` for an "
                "AI coding agent that has both active in its context at once. Pick the single "
                "option that best names the mechanism of the clash, or distinct/redundant "
                "when there is no clash."
            ),
            "co_activation": {
                "class": pair.co_active.value,
                "account": pair.co_activation_account,
                "precedence": f"{pair.precedence.kind.value}: {pair.precedence.account}",
            },
            "note": _COACT_NOTE,
        },
        "criteria": RELATION_CRITERIA,
    }


def _conflict_mass(probs: dict) -> float:
    return float(sum(probs.get(c, 0.0) for c in CONFLICT_CLASSES))


def _valid_probs(x: object) -> dict[str, float] | None:
    """A probability distribution as the API returns it, coerced to floats —
    or None for anything malformed, which is never cached and never trusted
    back out of the cache (one bad response must not poison every later scan)."""
    if not isinstance(x, dict) or not x:
        return None
    out: dict[str, float] = {}
    for k, v in x.items():
        if isinstance(v, bool) or not isinstance(v, (int, float, str)):
            return None
        try:
            f = float(v)
        except ValueError:
            return None
        if not (-1e-6 <= f <= 1 + 1e-6):
            return None
        out[str(k)] = f
    return out


def _sanitize(rec: object) -> dict | None:
    """A judged-pair record with both orderings validated; None means "ask again"."""
    if not isinstance(rec, dict):
        return None
    probs = _valid_probs(rec.get("probs"))
    swapped = _valid_probs(rec.get("probs_swapped"))
    if probs is None or swapped is None:
        return None
    return {"probs": probs, "probs_swapped": swapped, "batch": rec.get("batch")}


def _estimate_tokens(obj: object) -> int:
    return len(json.dumps(obj, ensure_ascii=False)) // 2


def _select_pairs(cfg: Config, ctx: AnalysisContext) -> tuple[list[UnitPair], bool]:
    """All co-activatable unit pairs (``pairs = "all"``) or the deterministic
    lane's unclaimed candidate pairs (``pairs = "candidates"``), one per pair
    key, and whether ``max_pairs`` cut the list short.

    Verbatim copies of one unit (same uid) are never paired with each other:
    a request keyed by uid could not tell the copies apart, and duplicated
    text is the deterministic DTR01 detector's territory."""

    def candidates():
        if cfg.typesafe_pairs == "candidates":
            for p in ctx.pairs:
                if not ctx.is_claimed(p) and p.a.uid != p.b.uid:
                    yield p
            return
        units = ctx.units
        for i in range(len(units)):
            for j in range(i + 1, len(units)):
                a, b = units[i], units[j]
                if a.uid == b.uid:
                    continue
                pair = build_pair(a, b)
                if pair.co_active == CoActiveClass.MUTUALLY_EXCLUSIVE or ctx.is_claimed(pair):
                    continue
                yield pair

    out: list[UnitPair] = []
    seen: set[str] = set()
    for pair in candidates():
        if pair.key in seen:
            continue  # a verbatim copy elsewhere in the file: same question, same verdict
        if len(out) >= cfg.typesafe_max_pairs:
            ctx.corpus.notes.append(
                f"typesafe lane: pair cap reached ({cfg.typesafe_max_pairs}); "
                "remaining pairs not judged"
            )
            return out, True
        seen.add(pair.key)
        out.append(pair)
    return out, False


def judge_pairs(
    client: TypeSafeClient,
    pairs: list[UnitPair],
    cache,
    pairs_per_call: int = 20,
    *,
    solo: bool = False,
) -> dict[str, dict]:
    """Return {pair.key: {"probs": {...}, "probs_swapped": {...}, "batch": n}} for
    every pair, batching uncached pairs into as few requests as the token
    budget allows. ``solo`` asks one pair per request (a two-unit state) under
    its own cache key, so a pair's batched and solo verdicts coexist."""
    suffix = "|swap-both|solo" if solo else "|swap-both"
    if solo:
        pairs_per_call = 1
    results: dict[str, dict] = {}
    pending: list[UnitPair] = []
    for p in pairs:
        hit = _sanitize(cache.get(cache.key(client.ident, _PROMPT_HASH, f"{p.key}{suffix}")))
        if hit is not None:
            results[p.key] = hit
        else:
            pending.append(p)  # never judged, or a malformed record: ask again

    while pending:
        chunk: list[UnitPair] = []
        used: dict[str, dict] = {}
        for p in pending:
            trial = dict(used)
            trial.setdefault(p.a.uid, _unit_record(p.a))
            trial.setdefault(p.b.uid, _unit_record(p.b))
            if chunk and (
                _estimate_tokens(trial) + (len(chunk) + 1) * 400 > _BUDGET_TOKENS
                or len(chunk) >= pairs_per_call
            ):
                break
            chunk.append(p)
            used = trial
        pending = pending[len(chunk) :]
        state = {"units": used}
        questions: dict = {}
        for p in chunk:
            questions[f"r_{p.a.uid}_{p.b.uid}"] = _question(p.a.uid, p.b.uid, p)
            questions[f"r_{p.b.uid}_{p.a.uid}"] = _question(p.b.uid, p.a.uid, p)
        answers = client.evaluate(state, questions)
        for p in chunk:
            fwd = answers.get(f"r_{p.a.uid}_{p.b.uid}")
            rev = answers.get(f"r_{p.b.uid}_{p.a.uid}")
            rec = _sanitize(
                {
                    "probs": fwd.get("probabilities") if isinstance(fwd, dict) else None,
                    "probs_swapped": rev.get("probabilities") if isinstance(rev, dict) else None,
                    "batch": len(chunk),
                }
            )
            if rec is None:
                continue  # malformed answer (either ordering): leave unjudged, never cached
            cache.put(cache.key(client.ident, _PROMPT_HASH, f"{p.key}{suffix}"), rec)
            results[p.key] = rec
        cache.save()
    return results


def _redundant(rec: dict) -> float:
    """Redundancy mass: the minimum over the two orderings."""
    probs = rec["probs"]
    swapped = rec.get("probs_swapped") or probs
    return min(float(probs.get("redundant", 0.0)), float(swapped.get("redundant", 0.0)))


def _mass(rec: dict) -> float:
    """Conflict mass of a judged pair for EMISSION: the minimum over the two
    orderings and, for a pair judged in both passes, over the batched and the
    solo verdict — every reading must see the conflict before it fires."""
    probs = rec["probs"]
    swapped = rec.get("probs_swapped") or probs
    m = min(_conflict_mass(probs), _conflict_mass(swapped))
    return min(m, rec["batched_mass"]) if "batched_mass" in rec else m


def _mass_max(rec: dict) -> float:
    """Conflict mass of a judged pair for CLEARING: the maximum over the two
    orderings — a pair leaves the uncertain band only when both orderings
    agree it is below it."""
    probs = rec["probs"]
    swapped = rec.get("probs_swapped") or probs
    return max(_conflict_mass(probs), _conflict_mass(swapped))


def _verdict(rec: dict) -> tuple[str | None, float, str]:
    """(code or None, conflict mass, class) from a judged pair record.

    Conflict mass is the minimum over the two orderings (position-bias guard).
    The class is the argmax over the MEAN of both orderings' distributions —
    the orderings' argmaxes flip on near-ties, the mean does not."""
    probs = rec["probs"]
    swapped = rec.get("probs_swapped") or probs
    mass = _mass(rec)
    mean = {c: (probs.get(c, 0.0) + swapped.get(c, 0.0)) / 2 for c in CONFLICT_CLASSES}
    cls = max(CONFLICT_CLASSES, key=lambda c: mean[c])
    # redundancy, like a conflict, must be seen by every reading of the pair
    redundant = min(_redundant(rec), rec.get("batched_redundant", 1.0))
    if redundant >= 0.7 and mass < 0.3:
        return "DTR01", redundant, "redundant"
    return _CODE_FOR[cls], mass, cls


def _overlay(pair: UnitPair, code: str, cls: str) -> str:
    """Mirror the deterministic router's structural routing on top of the
    semantic class: a clash between a conditionally-loaded layer (skill,
    subagent, tool description, plugin) and another layer is a cross-layer
    collision (DTP04); two path-scoped rules whose globs partially overlap are
    a precedence ambiguity (DTP02). Numeric clashes and soft tension keep
    their own code, as they do in the router."""
    if cls in _OVERLAY_EXEMPT:
        return code
    a, b = pair.a, pair.b
    if a.layer != b.layer and (a.layer in _CONDITIONAL_LAYERS or b.layer in _CONDITIONAL_LAYERS):
        return "DTP04"
    if (
        a.activation.mode == ActivationMode.PATH
        and b.activation.mode == ActivationMode.PATH
        and scope_relation(a, b) == "overlap"
    ):
        return "DTP02"
    return code


def run_typesafe_lane(cfg: Config, ctx: AnalysisContext, findings: list[Finding]) -> list[Finding]:
    try:
        client = TypeSafeClient(
            model=cfg.typesafe_model,
            api_key_env=cfg.typesafe_api_key_env,
            endpoint=cfg.typesafe_endpoint,
        )
    except JuryError as e:
        ctx.corpus.notes.append(f"{e} — lane skipped")
        return findings

    pairs, capped = _select_pairs(cfg, ctx)
    if not pairs:
        ctx.lanes_ran.add("typesafe")
        ctx.corpus.notes.append("typesafe lane: no pairs to judge")
        return findings
    cache = make_cache(cfg)
    try:
        judged = judge_pairs(client, pairs, cache, cfg.typesafe_pairs_per_call)
    except JuryError as e:
        # nothing is handed to the jury either: it keeps its own candidate
        # ranking rather than receiving an empty band
        ctx.corpus.notes.append(f"typesafe lane: {e}; lane incomplete")
        return findings

    # second pass: everything the batched pass put at or above the uncertain
    # band is re-asked alone. The solo verdict decides clearing and the class;
    # emission still needs the batched pass's agreement (the solo record
    # carries the batched masses, and _mass/_verdict take the minimum). A pair
    # that already had the request to itself is not asked again.
    rejudged = 0
    if judged and cfg.typesafe_rejudge:
        band = [
            p
            for p in pairs
            if p.key in judged
            and judged[p.key].get("batch") != 1
            and _mass_max(judged[p.key]) >= cfg.typesafe_uncertain_low
        ]
        try:
            solo = judge_pairs(client, band, cache, solo=True)
        except JuryError as e:
            ctx.corpus.notes.append(
                f"typesafe lane: solo re-judge failed ({e}); batched verdicts kept"
            )
            solo = {}
        for key, rec in solo.items():
            batched = judged[key]
            judged[key] = dict(
                rec, batched_mass=_mass(batched), batched_redundant=_redundant(batched)
            )
        rejudged = len(solo)

    emitted = 0
    uncertain: list[tuple[UnitPair, float]] = []
    for pair in pairs:
        rec = judged.get(pair.key)
        if rec is None:
            continue
        code, mass, cls = _verdict(rec)
        if code == "DTR01":
            findings.append(
                Finding(
                    code="DTR01",
                    message=f"TypeSafe judged these redundant (p={mass:.2f}).",
                    severity=Severity.ADVISORY,
                    evidence=pair_evidence(pair),
                    units=[pair.a, pair.b],
                    co_activation=pair.co_activation_account,
                    precedence=pair.precedence.account,
                    confidence=mass,
                    lanes=("typesafe",),
                )
            )
            ctx.claim(pair)
            emitted += 1
            continue
        if mass >= cfg.typesafe_tau:
            code = _overlay(pair, code, cls)
            strong = mass >= cfg.typesafe_strong and cls not in (
                "conditional_conflict",
                "goal_tension",
            )
            findings.append(
                Finding(
                    code=code,
                    message=(f"TypeSafe verdict {cls} (conflict probability {mass:.2f})."),
                    severity=Severity.WARNING if strong else Severity.ADVISORY,
                    evidence=pair_evidence(pair),
                    units=[pair.a, pair.b],
                    co_activation=pair.co_activation_account,
                    precedence=pair.precedence.account,
                    suggestion=(
                        "Reconcile the two instructions or scope each to the situation it "
                        "belongs to."
                    ),
                    confidence=mass,
                    lanes=("typesafe",),
                )
            )
            ctx.claim(pair)
            emitted += 1
        elif _mass_max(rec) >= cfg.typesafe_uncertain_low:
            uncertain.append((pair, _mass_max(rec)))
        else:
            ctx.cleared.add(pair.key)

    # complete = every selected pair judged and none cut by the cap; only then
    # may the baseline treat a missing TypeSafe finding as gone
    complete = len(judged) == len(pairs) and not capped
    if complete:
        ctx.lanes_ran.add("typesafe")
    # the uncertain band goes to the jury (if enabled) through the jury queue,
    # best-scored first; a confident TypeSafe verdict never reaches the jury.
    # An incomplete run hands nothing on: the jury keeps its own ranking.
    if cfg.lane_jury and complete:
        ctx.jury_queue = sorted(uncertain, key=lambda t: -t[1])
    ctx.corpus.notes.append(
        f"typesafe lane: judged {len(judged)} of {len(pairs)} pair(s) with {client.ident} in "
        f"{client.calls} call(s), {rejudged} re-judged alone; {emitted} finding(s), "
        f"{len(uncertain)} uncertain"
        + (" (handed to the jury)" if cfg.lane_jury and complete else "")
        + ("" if complete else " — lane incomplete")
    )
    return findings
