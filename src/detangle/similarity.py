"""Lightweight lexical similarity (pure Python, deterministic).

Used for blocking and duplicate detection. Deliberately not embeddings:
the deterministic core must run offline; the NLI/jury lanes add semantics.
"""

from __future__ import annotations

import re

from .lexicons import content_tokens


def token_set(text: str) -> frozenset[str]:
    return frozenset(content_tokens(text))


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def containment(a: frozenset[str], b: frozenset[str]) -> float:
    """|a ∩ b| / |smaller| — robust when one text is much shorter."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _shingles(text: str, k: int = 3) -> frozenset[str]:
    norm = re.sub(r"\W+", " ", text.lower()).strip()
    norm = re.sub(r"\s+", " ", norm)
    if len(norm) < k:
        return frozenset([norm] if norm else [])
    return frozenset(norm[i : i + k] for i in range(len(norm) - k + 1))


def char_similarity(a: str, b: str, k: int = 3) -> float:
    """Character k-gram Jaccard — catches morphological variants."""
    sa, sb = _shingles(a, k), _shingles(b, k)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def text_similarity(a: str, b: str) -> float:
    """Blend of token Jaccard and char-shingle similarity in [0, 1]."""
    ta, tb = token_set(a), token_set(b)
    return 0.6 * jaccard(ta, tb) + 0.4 * char_similarity(a, b)
