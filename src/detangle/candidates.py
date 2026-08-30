"""Candidate-pair generation: multi-pass blocking, then co-activation pruning.

Blocking is a UNION of passes (conflicts are not near-duplicates — a pair
related only through a shared resource or trigger must still surface):

1. shared topic tag
2. shared frame action, or shared frame object head
3. shared quantity subject/dimension
4. shared defined term
5. lexical-similarity neighbors above the config threshold

Pairs whose co-activation is provably impossible are pruned before
detectors run (exact, free, and per the research eliminates the majority
of the quadratic space). Detectors that *want* excluded pairs (shadowed
names, divergent interpretation) work from the corpus directly.
"""

from __future__ import annotations

from collections import defaultdict
from itertools import combinations

from .activation import build_pair
from .config import Config
from .ir import CoActiveClass, InstructionUnit, UnitPair
from .similarity import text_similarity, token_set


def _block_maps(units: list[InstructionUnit]) -> dict[str, list[int]]:
    """key -> unit indexes, across all blocking passes."""
    blocks: dict[str, list[int]] = defaultdict(list)
    for i, u in enumerate(units):
        for t in u.topics:
            blocks[f"topic:{t}"].append(i)
        if u.frame.action:
            blocks[f"action:{u.frame.action}"].append(i)
        if u.frame.obj:
            head = u.frame.obj.split()[0]
            blocks[f"object:{head}"].append(i)
        for q in u.quantities:
            if q.subject:
                blocks[f"qty:{q.subject}"].append(i)
        for term in u.defined_terms:
            blocks[f"term:{term}"].append(i)
    return blocks


_MAX_BLOCK_SIZE = 200  # a block this large is a stopword-like key; sample it


def generate_pairs(units: list[InstructionUnit], cfg: Config) -> list[UnitPair]:
    """All candidate pairs surviving blocking + co-activation pruning."""
    n = len(units)
    if n < 2:
        return []

    candidate_keys: dict[tuple[int, int], set[str]] = defaultdict(set)

    blocks = _block_maps(units)
    for key, members in blocks.items():
        if len(members) > _MAX_BLOCK_SIZE:
            members = members[:_MAX_BLOCK_SIZE]
        for i, j in combinations(sorted(set(members)), 2):
            candidate_keys[(i, j)].add(key)

    # similarity pass: token-overlap prefilter via inverted index, then score
    tokens = [token_set(u.normalized) for u in units]
    inv: dict[str, list[int]] = defaultdict(list)
    for i, ts in enumerate(tokens):
        for t in ts:
            inv[t].append(i)
    sim_seen: set[tuple[int, int]] = set()
    for _t, members in inv.items():
        if len(members) > _MAX_BLOCK_SIZE:
            continue
        for i, j in combinations(members, 2):
            if (i, j) in sim_seen:
                continue
            sim_seen.add((i, j))

    similarities: dict[tuple[int, int], float] = {}
    for i, j in set(candidate_keys) | sim_seen:
        s = text_similarity(units[i].text, units[j].text)
        if s >= cfg.similarity_threshold:
            candidate_keys[(i, j)].add("similarity")
        if (i, j) in candidate_keys:
            similarities[(i, j)] = s

    # build pairs with co-activation + precedence accounts, prune exclusives
    pairs: list[UnitPair] = []
    for (i, j), keys in sorted(candidate_keys.items()):
        a, b = units[i], units[j]
        if a.uid == b.uid:
            continue
        pair = build_pair(a, b, similarity=similarities.get((i, j), 0.0))
        pair.block_keys = tuple(sorted(keys))
        if pair.co_active == CoActiveClass.MUTUALLY_EXCLUSIVE:
            continue
        pairs.append(pair)
        if len(pairs) >= cfg.max_pairs:
            break
    return pairs
