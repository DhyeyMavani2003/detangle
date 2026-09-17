"""TypeSafe lane: calibrated pairwise conflict judgments via typed questions.

Where the jury asks a generative model to *write* a verdict and we parse it,
this lane asks a System One decision model (TypeSafe's Jev) a typed Choice
question per instruction pair and reads back a probability distribution over
relationship classes. Two properties make it a different kind of lane:

- **Batching.** Every question in a request sees the same ``state`` and is
  evaluated in parallel, so a whole config's units go in as state once and
  hundreds of pair questions ride along — a 49-tree holdout adjudicates in
  seconds, not hours.
- **Calibration.** Emission is thresholded on the returned probabilities, and
  on the holdout the lane reached the deep opus+opus cascade's recall with
  zero false positives at every threshold tried (docs/lanes.md).

Each pair is asked in BOTH orderings inside the same request (the jury's
order-swap guard, at no extra round trip); the conflict mass used for
emission is the minimum across the two, and a class disagreement softens
the code to the conditional reading.

Composability: pairs judged confidently are claimed; pairs whose conflict
mass lands in the uncertain band are handed to the jury lane (when enabled)
through the same channel the NLI lane uses, so ``--typesafe --jury`` means
"TypeSafe decides what it can; a generative juror handles the rest".

Findings carry ``lanes: ["typesafe"]``. Verdicts are cached per pair by
(linter version, model, prompt hash, pair key), so re-scans of unchanged
configs make zero calls.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request

from ..activation import build_pair
from ..config import Config
from ..detectors.base import AnalysisContext
from ..findings import Finding, pair_evidence
from ..ir import CoActiveClass, InstructionUnit, UnitPair
from ..taxonomy import Severity
from .backends import JuryError
from .cachekey import make_cache

DEFAULT_ENDPOINT = "https://api.typesafe.ai/v1/systemone"

# Relationship vocabulary: one atomic Choice per pair. Keys are the API
# options; values are the rubric the model sees. Order-independent.
RELATION_CRITERIA = {
    "contradictory": (
        "Incompatible prescriptions for the same situation; following one forces "
        "violating the other (including opposite step orders: A before B vs B before A)."
    ),
    "conditional_conflict": (
        "Each satisfiable alone, but jointly unsatisfiable when a specific condition holds."
    ),
    "numeric_limit_conflict": (
        "Both set a numeric limit for the same quantity and the limits cannot both hold."
    ),
    "format_conflict": "They require mutually exclusive output formats for the same artifact.",
    "permit_vs_forbid": "One explicitly permits an action the other explicitly forbids.",
    "redundant": "The same prescription stated twice in different words (entail each other).",
    "distinct": "Compatible: different matters, or one refines/scopes the other without contradiction.",
}
CONFLICT_CLASSES = (
    "contradictory",
    "conditional_conflict",
    "numeric_limit_conflict",
    "format_conflict",
    "permit_vs_forbid",
)
_CODE_FOR = {
    "contradictory": "DTC01",
    "conditional_conflict": "DTC02",
    "numeric_limit_conflict": "DTC03",
    "format_conflict": "DTC04",
    "permit_vs_forbid": "DTC05",
}

_PROMPT_VERSION = "relation-v1"
_PROMPT_HASH = hashlib.sha256(
    (_PROMPT_VERSION + json.dumps(RELATION_CRITERIA, sort_keys=True)).encode()
).hexdigest()[:12]

# request sizing: the API budget is ~32k tokens shared by state + questions
# (chars/2 is a pessimistic token estimate). Pairs per call is a CONFIG knob
# because judgment quality measurably degrades as the shared state grows —
# see docs/lanes.md — so small batches buy recall.
_BUDGET_TOKENS = 14_000


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
                answers = payload.get("answers")
                if not isinstance(answers, dict):
                    raise JuryError(f"typesafe: unexpected response shape: {str(payload)[:200]}")
                return answers
            except urllib.error.HTTPError as e:
                detail = e.read().decode("utf-8", "replace")[:300]
                if e.code in (429, 529) and attempt < 5:
                    time.sleep(delay)
                    delay = min(delay * 2, 30)
                    continue
                raise JuryError(f"typesafe HTTP {e.code}: {detail}") from e
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
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


def _question(a_id: str, b_id: str) -> dict:
    return {
        "type": "choice",
        "instructions": (
            f"Classify the relationship between `units.{a_id}` and `units.{b_id}` for an AI "
            "coding agent that has both active in its context at once."
        ),
        "criteria": RELATION_CRITERIA,
    }


def _conflict_mass(probs: dict) -> float:
    return float(sum(probs.get(c, 0.0) for c in CONFLICT_CLASSES))


def _estimate_tokens(obj: object) -> int:
    return len(json.dumps(obj, ensure_ascii=False)) // 2


def _select_pairs(cfg: Config, ctx: AnalysisContext) -> list[UnitPair]:
    """All co-activatable unit pairs (``pairs = "all"``) or the deterministic
    lane's unclaimed candidate pairs (``pairs = "candidates"``)."""
    if cfg.typesafe_pairs == "candidates":
        return [p for p in ctx.pairs if not ctx.is_claimed(p)]
    units = ctx.units
    out: list[UnitPair] = []
    for i in range(len(units)):
        for j in range(i + 1, len(units)):
            a, b = units[i], units[j]
            if a.uid == b.uid and a.span.start_line == b.span.start_line:
                continue
            pair = build_pair(a, b)
            if pair.co_active == CoActiveClass.MUTUALLY_EXCLUSIVE or ctx.is_claimed(pair):
                continue
            out.append(pair)
            if len(out) >= cfg.typesafe_max_pairs:
                ctx.corpus.notes.append(
                    f"typesafe lane: pair cap reached ({cfg.typesafe_max_pairs}); "
                    "remaining pairs not judged"
                )
                return out
    return out


def judge_pairs(
    client: TypeSafeClient, pairs: list[UnitPair], cache, pairs_per_call: int = 20
) -> dict[str, dict]:
    """Return {pair.key: {"probs": {...}, "probs_swapped": {...}}} for every pair,
    batching uncached pairs into as few requests as the token budget allows."""
    results: dict[str, dict] = {}
    pending: list[UnitPair] = []
    for p in pairs:
        hit = cache.get(cache.key(client.ident, _PROMPT_HASH, f"{p.key}|swap-both"))
        if hit is not None:
            results[p.key] = hit
        else:
            pending.append(p)

    while pending:
        chunk: list[UnitPair] = []
        used: dict[str, dict] = {}
        for p in pending:
            trial = dict(used)
            trial.setdefault(p.a.uid, _unit_record(p.a))
            trial.setdefault(p.b.uid, _unit_record(p.b))
            if chunk and (
                _estimate_tokens(trial) + (len(chunk) + 1) * 150 > _BUDGET_TOKENS
                or len(chunk) >= pairs_per_call
            ):
                break
            chunk.append(p)
            used = trial
        pending = pending[len(chunk) :]
        state = {"units": used}
        questions: dict = {}
        for p in chunk:
            questions[f"r_{p.a.uid}_{p.b.uid}"] = _question(p.a.uid, p.b.uid)
            questions[f"r_{p.b.uid}_{p.a.uid}"] = _question(p.b.uid, p.a.uid)
        answers = client.evaluate(state, questions)
        for p in chunk:
            fwd = answers.get(f"r_{p.a.uid}_{p.b.uid}") or {}
            rev = answers.get(f"r_{p.b.uid}_{p.a.uid}") or {}
            if not isinstance(fwd.get("probabilities"), dict):
                continue  # malformed answer: leave unjudged (never cached)
            rec = {
                "probs": fwd["probabilities"],
                "probs_swapped": rev.get("probabilities")
                if isinstance(rev.get("probabilities"), dict)
                else None,
            }
            cache.put(cache.key(client.ident, _PROMPT_HASH, f"{p.key}|swap-both"), rec)
            results[p.key] = rec
        cache.save()
    return results


def _verdict(rec: dict) -> tuple[str | None, float, str]:
    """(code or None, conflict mass, class) from a judged pair record."""
    probs = rec["probs"]
    mass = _conflict_mass(probs)
    cls = max(CONFLICT_CLASSES, key=lambda c: probs.get(c, 0.0))
    swapped = rec.get("probs_swapped")
    if swapped:
        mass = min(mass, _conflict_mass(swapped))
        cls_sw = max(CONFLICT_CLASSES, key=lambda c: swapped.get(c, 0.0))
        if cls_sw != cls:
            # the orderings agree a conflict exists but not on its flavor:
            # take the weaker conditional reading (mirrors the jury's rule)
            cls = "conditional_conflict"
    redundant = float(probs.get("redundant", 0.0))
    if swapped:
        redundant = min(redundant, float(swapped.get("redundant", 0.0)))
    if redundant >= 0.6 and mass < 0.3:
        return "DTR01", redundant, "redundant"
    return _CODE_FOR[cls], mass, cls


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

    pairs = _select_pairs(cfg, ctx)
    if not pairs:
        ctx.lanes_ran.add("typesafe")
        ctx.corpus.notes.append("typesafe lane: no pairs to judge")
        return findings
    cache = make_cache(cfg)
    try:
        judged = judge_pairs(client, pairs, cache, cfg.typesafe_pairs_per_call)
    except JuryError as e:
        ctx.corpus.notes.append(f"typesafe lane: {e}; lane incomplete")
        judged = {}

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
            strong = mass >= cfg.typesafe_strong and cls != "conditional_conflict"
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
        elif mass >= cfg.typesafe_uncertain_low:
            uncertain.append((pair, mass))

    if judged and len(judged) == len(pairs):
        ctx.lanes_ran.add("typesafe")
    # the uncertain band goes to the jury (if enabled) through the NLI channel,
    # best-scored first; a confident TypeSafe verdict never reaches the jury
    if cfg.lane_jury and getattr(ctx, "nli_not_cleared", None) is None:
        ctx.nli_not_cleared = sorted(uncertain, key=lambda t: -t[1])
    ctx.corpus.notes.append(
        f"typesafe lane: judged {len(judged)} pair(s) with {client.ident} in "
        f"{client.calls} call(s); {emitted} finding(s), {len(uncertain)} uncertain"
        + (" (handed to the jury)" if cfg.lane_jury else "")
    )
    return findings
