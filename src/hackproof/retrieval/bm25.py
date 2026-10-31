"""Stdlib BM25-lite keyword channel for hybrid retrieval (Step 4.3b.2).

No third-party dependencies (deliberately): keeps the ``retrieval`` extra
unchanged and the offline path hermetic. Standard BM25 (k1=1.2, b=0.75)
over lowercased ``[a-z0-9]+`` tokens.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Sequence

__all__ = ["tokenize", "BM25Index"]

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_K1 = 1.2
_B = 0.75


def tokenize(text: str) -> list[str]:
    """Lowercased alphanumeric tokens (underscores split)."""
    return _TOKEN_RE.findall(text.lower())


class BM25Index:
    """BM25 over a fixed document corpus."""

    def __init__(self, documents: Sequence[str]) -> None:
        self._docs = [tokenize(doc) for doc in documents]
        self._lengths = [len(tokens) for tokens in self._docs]
        count = len(self._docs)
        self._avg_len = (sum(self._lengths) / count) if count else 0.0
        doc_freq: Counter[str] = Counter()
        for tokens in self._docs:
            doc_freq.update(set(tokens))
        self._idf = {
            term: math.log(1.0 + (count - freq + 0.5) / (freq + 0.5))
            for term, freq in doc_freq.items()
        }

    def scores(self, query: str) -> list[float]:
        """BM25 score per document (zero when nothing matches)."""
        query_terms = tokenize(query)
        result: list[float] = []
        for tokens, doc_len in zip(self._docs, self._lengths):
            if not tokens or not query_terms:
                result.append(0.0)
                continue
            counts = Counter(tokens)
            norm = 1.0 - _B + _B * (doc_len / self._avg_len) if self._avg_len else 1.0
            total = 0.0
            for term in set(query_terms):
                freq = counts.get(term, 0)
                if not freq:
                    continue
                total += self._idf.get(term, 0.0) * (
                    freq * (_K1 + 1.0) / (freq + _K1 * norm)
                )
            result.append(total)
        return result

    def top(self, query: str, n: int) -> list[tuple[int, float]]:
        """Top-n (doc_index, score) pairs, best first, positives only."""
        ranked = sorted(
            ((idx, score) for idx, score in enumerate(self.scores(query)) if score > 0.0),
            key=lambda pair: (-pair[1], pair[0]),
        )
        return ranked[: max(n, 0)]
