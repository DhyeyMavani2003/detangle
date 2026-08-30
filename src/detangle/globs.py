"""Glob matching, intersection, and subset tests for scope-overlap analysis.

Agent-config ecosystems use (at least) gitignore-ish and minimatch-ish glob
dialects. For co-activation we need more than matching: we need to decide
whether two globs can select a *common* path (intersection non-emptiness)
and whether one glob's scope fully covers another's (subset, approximate).

Intersection is exact for the supported pattern language (literals, ``?``,
``*``, ``**``, brace expansion; character classes are approximated as ``?``,
which can only over-report overlap — the safe direction for a linter that
reports "potentially co-active").

Subset testing is heuristic (structural rules + adversarial sampling); it is
used only to distinguish "fully shadowed" (DTP01) from "partially
overlapping" (DTP02/DTP03), and callers phrase findings accordingly.
"""

from __future__ import annotations

import re
from functools import cache, lru_cache

_MAX_BRACE_EXPANSIONS = 64


def expand_braces(pattern: str) -> list[str]:
    """Expand {a,b} alternations (one level at a time, bounded)."""
    out = [pattern]
    changed = True
    while changed and len(out) <= _MAX_BRACE_EXPANSIONS:
        changed = False
        nxt: list[str] = []
        for p in out:
            m = re.search(r"\{([^{}]*)\}", p)
            if m:
                changed = True
                for alt in m.group(1).split(","):
                    nxt.append(p[: m.start()] + alt + p[m.end() :])
            else:
                nxt.append(p)
        out = nxt
    return out[:_MAX_BRACE_EXPANSIONS]


def _norm(pattern: str) -> str:
    p = pattern.strip().replace("\\", "/")
    if p.startswith("./"):
        p = p[2:]
    p = p.lstrip("/")
    # 'dir/' means everything under dir
    if p.endswith("/"):
        p = p + "**"
    return p


def _bare(pattern: str) -> bool:
    """gitignore-ish: a pattern with no separator other than an optional
    trailing one ('build', 'build/') matches at any depth."""
    p = pattern.strip().replace("\\", "/")
    if p.startswith("./"):
        p = p[2:]
    p = p.lstrip("/")
    return "/" not in p.rstrip("/")


def _class_end(s: str, j: int) -> int:
    """Index of the closing ']' for a char class opening at ``s[j]`` (-1 when
    unterminated). POSIX/fnmatch: a ']' first in the class body (after an
    optional '!' negation) is a literal member, not the terminator."""
    k = j + 1
    if k < len(s) and s[k] == "!":
        k += 1
    if k < len(s) and s[k] == "]":
        k += 1
    return s.find("]", k)


@lru_cache(maxsize=4096)
def _glob_regex(pattern: str) -> re.Pattern[str]:
    """Compile a single (brace-free) glob to a full-path regex."""
    parts = _norm(pattern).split("/")
    rx: list[str] = []
    for i, part in enumerate(parts):
        if part == "**":
            # '**/' matches zero or more directories
            rx.append("(?:[^/]+/)*" if i < len(parts) - 1 else ".*")
            continue
        seg = ""
        j = 0
        while j < len(part):
            c = part[j]
            if c == "*":
                seg += "[^/]*"
            elif c == "?":
                seg += "[^/]"
            elif c == "[":
                k = _class_end(part, j)
                if k == -1:
                    seg += re.escape(c)
                else:
                    body = part[j + 1 : k]
                    neg = body.startswith("!")
                    if neg:
                        body = body[1:]
                    seg += "[" + ("^" if neg else "") + body.replace("]", "\\]") + "]"
                    j = k
            else:
                seg += re.escape(c)
            j += 1
        rx.append(seg + ("/" if i < len(parts) - 1 else ""))
    # collapse the '**/' construction: it already includes its trailing slash
    joined = "".join(r if r.endswith("/") or r in {".*"} or r.startswith("(?:") else r for r in rx)
    return re.compile("^" + joined + "$")


def glob_match(pattern: str, path: str) -> bool:
    """Match a repo-relative posix path against a glob.

    Follows gitignore-ish semantics for bare patterns: a pattern with no
    ``/`` matches at any depth (equivalent to ``**/pattern``).
    """
    path = path.lstrip("/")
    for pat in expand_braces(pattern):
        bare = _bare(pat)
        pat = _norm(pat)
        candidates = [pat]
        if bare:
            candidates.append(f"**/{pat}")
        for c in candidates:
            try:
                if _glob_regex(c).match(path):
                    return True
            except re.error:
                if pat == path:
                    return True
    return False


def any_glob_match(patterns: tuple[str, ...] | list[str], path: str) -> bool:
    return any(glob_match(p, path) for p in patterns)


# ---------------------------------------------------------------------------
# Intersection: can two globs match a common path?
# ---------------------------------------------------------------------------


def _tokenize_segment(seg: str) -> list[str]:
    """Segment -> tokens: literal chars, '?', '*'. Char classes -> '?'."""
    toks: list[str] = []
    j = 0
    while j < len(seg):
        c = seg[j]
        if c == "*":
            # collapse runs of '*'
            if not toks or toks[-1] != "*":
                toks.append("*")
        elif c == "?":
            toks.append("?")
        elif c == "[":
            k = _class_end(seg, j)
            if k == -1:
                toks.append(c)
            else:
                toks.append("?")
                j = k
        else:
            toks.append(c)
        j += 1
    return toks


def _segments_unify(a: str, b: str) -> bool:
    """Do wildcard segment patterns a and b share a common string?"""
    ta, tb = _tokenize_segment(a), _tokenize_segment(b)

    @cache
    def ok(i: int, j: int) -> bool:
        if i == len(ta) and j == len(tb):
            return True
        if i < len(ta) and ta[i] == "*":
            # star matches empty, or absorbs one symbol emitted by b's side
            if ok(i + 1, j):
                return True
            if j < len(tb):
                # b's next token emits >=0 chars; sliding past it keeps validity
                return ok(i, j + 1)
            return False
        if j < len(tb) and tb[j] == "*":
            if ok(i, j + 1):
                return True
            if i < len(ta):
                return ok(i + 1, j)
            return False
        if i == len(ta) or j == len(tb):
            return False
        ca, cb = ta[i], tb[j]
        if ca == "?" or cb == "?" or ca == cb:
            return ok(i + 1, j + 1)
        return False

    return ok(0, 0)


def _split_segments(pattern: str) -> list[str]:
    return _norm(pattern).split("/")


def _paths_unify(sa: list[str], sb: list[str]) -> bool:
    """Segment-level DP: '**' matches zero or more whole segments."""

    @cache
    def ok(i: int, j: int) -> bool:
        if i == len(sa) and j == len(sb):
            return True
        if i < len(sa) and sa[i] == "**":
            if ok(i + 1, j):
                return True
            if j < len(sb):
                return ok(i, j + 1)
            return False
        if j < len(sb) and sb[j] == "**":
            if ok(i, j + 1):
                return True
            if i < len(sa):
                return ok(i + 1, j)
            return False
        if i == len(sa) or j == len(sb):
            return False
        if _segments_unify(sa[i], sb[j]):
            return ok(i + 1, j + 1)
        return False

    return ok(0, 0)


def globs_intersect(a: str, b: str) -> bool:
    """Can globs a and b match a common path? Exact for the supported language."""
    for pa in expand_braces(a):
        for pb in expand_braces(b):
            na, nb = _norm(pa), _norm(pb)
            # bare patterns (trailing-slash dir globs included) match at any depth
            cand_a = [na, f"**/{na}"] if _bare(pa) else [na]
            cand_b = [nb, f"**/{nb}"] if _bare(pb) else [nb]
            for ca in cand_a:
                for cb in cand_b:
                    if _paths_unify(_split_segments(ca), _split_segments(cb)):
                        return True
    return False


def glob_sets_intersect(a: tuple[str, ...] | list[str], b: tuple[str, ...] | list[str]) -> bool:
    return any(globs_intersect(x, y) for x in a for y in b)


# ---------------------------------------------------------------------------
# Subset (approximate): does every path matched by `sub` also match `sup`?
# ---------------------------------------------------------------------------


def _class_members(body: str) -> list[str]:
    """Concrete candidate characters for a ``[...]`` class body (callers
    validate the resulting samples, so guesses for negated classes are safe)."""
    if body.startswith("!"):
        return ["z", "q", "9"]
    members: list[str] = []
    j = 0
    while j < len(body):
        members.append(body[j])
        j += 3 if body[j + 1 : j + 2] == "-" else 1
    return members or ["x"]


def _segment_variants(seg: str) -> list[str]:
    """A few concrete strings for one glob segment. Fillers are deliberately
    distinct across variants ('?' -> 'x'/'q'/'z') so a literal in a candidate
    superset pattern cannot accidentally cover every sample."""
    variants: list[str] = []
    for star, qmark, pick in (("x", "x", 0), ("zq9", "q", 1), ("", "z", 0)):
        parts: list[str] = []
        j = 0
        while j < len(seg):
            c = seg[j]
            if c == "*":
                parts.append(star)
            elif c == "?":
                parts.append(qmark)
            elif c == "[":
                k = _class_end(seg, j)
                if k == -1:
                    parts.append(c)
                else:
                    members = _class_members(seg[j + 1 : k])
                    parts.append(members[min(pick, len(members) - 1)])
                    j = k
            else:
                parts.append(c)
            j += 1
        variants.append("".join(parts))
    return [v for v in dict.fromkeys(variants) if v]


def _samples(pattern: str) -> list[str]:
    """Representative concrete paths matched by a glob.

    Every returned sample is verified with ``glob_match(pattern, sample)``;
    an empty result means sampling failed and callers must not conclude
    anything from it (in particular, never claim subset)."""
    out: set[str] = set()
    for pat in expand_braces(pattern)[:8]:
        segs = _split_segments(pat)
        base: list[list[str]] = [[]]
        for seg in segs:
            nxt: list[list[str]] = []
            if seg == "**":
                for b in base:
                    nxt.append(list(b))
                    nxt.append([*b, "sub"])
                    nxt.append([*b, "sub", "dir"])
            else:
                for v in _segment_variants(seg):
                    for b in base:
                        nxt.append([*b, v])
            base = nxt[:64]
        for b in base:
            if b:
                out.add("/".join(b))
    return [s for s in sorted(out)[:128] if glob_match(pattern, s)]


def glob_subset(sub: str, sup: str) -> bool:
    """Heuristic: does ``sup`` cover everything ``sub`` matches?

    Structural fast paths, then adversarial sampling over verified samples.
    False negatives are possible; callers must treat True as "likely fully
    covered". When no verifiable sample exists the answer is False — subset
    is never claimed unchecked.
    """
    ns, np_ = _norm(sub), _norm(sup)
    if ns == np_:
        return True
    if np_ in {"**", "**/*"}:
        return True
    if not globs_intersect(sub, sup):
        return False
    samples = _samples(sub)
    if not samples:
        return False
    return all(glob_match(sup, s) for s in samples)


def glob_set_subset(sub: tuple[str, ...] | list[str], sup: tuple[str, ...] | list[str]) -> bool:
    """Every glob in ``sub`` is covered by some combination of ``sup``."""
    if not sub or not sup:
        return False
    for s in sub:
        if any(glob_subset(s, p) for p in sup):
            continue
        smps = _samples(s)
        if not smps or not all(any_glob_match(list(sup), smp) for smp in smps):
            return False
    return True
