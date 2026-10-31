"""Rank fusion and test/doc downranking for hybrid retrieval (4.3b.2).

Reciprocal-rank fusion (RRF, k=60) merges dense and keyword channels by
rank, so uncalibrated cross-channel scores never mix. Test/documentation
chunks are downranked (never excluded): source files win ties while the
product stays usable for doc-only repositories.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import PurePosixPath

__all__ = [
    "DOWNRANK_FACTOR",
    "is_test_or_doc_path",
    "downrank_test_and_doc",
    "rrf_fuse",
]

DOWNRANK_FACTOR = 0.5
_RRF_K = 60

_TEST_DIR_PARTS = frozenset(
    {
        "tests",
        "test",
        "testing",
        "__tests__",
        "doc",
        "docs",
        "bench",
        "benchmarks",
        "examples",
        "example",
    }
)


def is_test_or_doc_path(path: str) -> bool:
    """True for test, doc-example, or benchmark paths (heuristic)."""
    parts = PurePosixPath(path).parts
    if not parts:
        return False
    if any(part in _TEST_DIR_PARTS for part in parts[:-1]):
        return True
    base = parts[-1]
    stem = base.rsplit(".", 1)[0]
    return (
        base == "conftest.py"
        or stem.startswith("test_")
        or stem.endswith("_test")
        or ".spec." in base
        or ".test." in base
    )


def downrank_test_and_doc(
    ranked: Sequence[tuple[str, float]],
) -> list[tuple[str, float]]:
    """Apply the test/doc penalty and re-sort best-first (stable)."""
    penalized = [
        (path, score * DOWNRANK_FACTOR if is_test_or_doc_path(path) else score)
        for path, score in ranked
    ]
    return sorted(penalized, key=lambda pair: -pair[1])


def rrf_fuse(
    rankings: Sequence[Sequence[str]], k: int = _RRF_K
) -> list[tuple[str, float]]:
    """Fuse rank lists by reciprocal rank (deterministic on ties)."""
    fused: dict[str, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            fused[doc_id] = fused.get(doc_id, 0.0) + 1.0 / (k + rank)
    return sorted(fused.items(), key=lambda pair: (-pair[1], pair[0]))
