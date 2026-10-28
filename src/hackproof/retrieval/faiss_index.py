"""In-memory code index: Faiss ``IndexFlatIP`` with a numpy fallback.

Vectors are L2-normalized 768-dim embeddings, so inner product equals
cosine similarity on both backends.
"""

from __future__ import annotations

from typing import Any, Sequence

import numpy as np

try:
    import faiss

    _FAISS_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised when faiss is missing
    faiss = None  # type: ignore[assignment]
    _FAISS_AVAILABLE = False

INDEX_DIM = 768

__all__ = ["INDEX_DIM", "FaissCodeIndexer"]


def _as_unit_matrix(vectors: np.ndarray, dim: int) -> np.ndarray:
    """Validate shape and defensively L2-normalize rows to float32."""
    matrix = np.asarray(vectors, dtype=np.float32)
    if matrix.ndim != 2 or matrix.shape[1] != dim:
        raise ValueError(f"embeddings must have shape (n, {dim})")
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return (matrix / norms).astype(np.float32)


class FaissCodeIndexer:
    """Top-k code search over normalized dense vectors.

    Args:
        dim: Embedding dimension (default 768).
        backend: ``"auto"`` (Faiss when installed, else numpy),
            ``"faiss"``, or ``"numpy"``.
    """

    def __init__(self, dim: int = INDEX_DIM, backend: str = "auto") -> None:
        if backend == "auto":
            backend = "faiss" if _FAISS_AVAILABLE else "numpy"
        if backend == "faiss":
            if not _FAISS_AVAILABLE:
                raise ImportError(
                    "faiss is not installed; use backend='numpy' or "
                    "install the 'retrieval' extra"
                )
            assert faiss is not None
            self._index = faiss.IndexFlatIP(dim)
            self._matrix: np.ndarray | None = None
        elif backend == "numpy":
            self._index = None
            self._matrix = np.zeros((0, dim), dtype=np.float32)
        else:
            raise ValueError("backend must be one of 'auto', 'faiss', 'numpy'")
        self.dim = dim
        self.backend = backend
        self._chunks: list[dict[str, Any]] = []

    def __len__(self) -> int:
        return len(self._chunks)

    def add_chunks(
        self, chunks: Sequence[dict[str, Any]], embeddings: np.ndarray
    ) -> None:
        """Append chunks with their row-aligned embedding matrix."""
        chunk_list = list(chunks)
        matrix = _as_unit_matrix(embeddings, self.dim)
        if len(chunk_list) != matrix.shape[0]:
            raise ValueError("chunks and embeddings must have equal length")
        if self._index is not None:
            self._index.add(matrix)
        else:
            assert self._matrix is not None
            self._matrix = np.vstack([self._matrix, matrix])
        self._chunks.extend(chunk_list)

    def search(
        self, query_embedding: np.ndarray, top_k: int = 5
    ) -> list[tuple[dict[str, Any], float]]:
        """Return ``[(chunk, score)]`` top-k pairs, best first."""
        if top_k < 1:
            raise ValueError("top_k must be >= 1")
        if not self._chunks:
            return []
        query = _as_unit_matrix(
            np.asarray(query_embedding, dtype=np.float32).reshape(1, -1), self.dim
        )
        limit = min(top_k, len(self._chunks))
        if self._index is not None:
            scores, positions = self._index.search(query, limit)
            scores, positions = scores[0], positions[0]
        else:
            assert self._matrix is not None
            similarities = self._matrix @ query[0]
            if limit >= len(self._chunks):
                order = np.argsort(-similarities)
            else:
                part = np.argpartition(-similarities, limit - 1)[:limit]
                order = part[np.argsort(-similarities[part])]
            positions, scores = order, similarities[order]
        return [
            (self._chunks[int(position)], float(score))
            for position, score in zip(positions, scores)
        ]
