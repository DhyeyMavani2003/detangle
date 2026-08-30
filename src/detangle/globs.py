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
                k = part.find("]", j + 1)
                if k == -1:
                    seg += re.escape(c)
                else:
                    body = part[j + 1 : k]
                    if body.startswith("!"):
                        body = "^" + body[1:]
                    seg += f"[{body}]"
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
        pat = _norm(pat)
        candidates = [pat]
        if "/" not in pat:
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
            k = seg.find("]", j + 1)
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
            # bare patterns match at any depth
            cand_a = [na] if "/" in na else [na, f"**/{na}"]
            cand_b = [nb] if "/" in nb else [nb, f"**/{nb}"]
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

_SAMPLE_FILLERS = ("x", "zq9", "a-b_c", "deep/nested/f", "UPPER")


def _samples(pattern: str) -> list[str]:
    """Generate representative concrete paths matched by a glob."""
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
                variants = set()
                for filler in ("x", "zq9"):
                    variants.add(
                        "".join(
                            filler if t == "*" else ("x" if t == "?" else t)
                            for t in _tokenize_segment(seg)
                        )
                    )
                # '*' also matches empty
                variants.add(
                    "".join(
                        "x" if t == "?" else ("" if t == "*" else t) for t in _tokenize_segment(seg)
                    )
                )
                for v in variants:
                    if v:
                        for b in base:
                            nxt.append([*b, v])
            base = nxt[:64]
        for b in base:
            if b:
                out.add("/".join(b))
    return sorted(out)[:128]


def glob_subset(sub: str, sup: str) -> bool:
    """Heuristic: does ``sup`` cover everything ``sub`` matches?

    Structural fast paths, then adversarial sampling. False negatives are
    possible; callers must treat True as "likely fully covered".
    """
    ns, np_ = _norm(sub), _norm(sup)
    if ns == np_:
        return True
    if np_ in {"**", "**/*"}:
        return True
    if not globs_intersect(sub, sup):
        return False
    return all(glob_match(sup, s) for s in _samples(sub))


def glob_set_subset(sub: tuple[str, ...] | list[str], sup: tuple[str, ...] | list[str]) -> bool:
    """Every glob in ``sub`` is covered by some combination of ``sup``."""
    if not sub or not sup:
        return False
    return all(
        any(glob_subset(s, p) for p in sup)
        or all(any_glob_match(list(sup), smp) for smp in _samples(s))
        for s in sub
    )
