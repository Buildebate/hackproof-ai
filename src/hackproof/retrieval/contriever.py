"""CPU-first dense embeddings for code retrieval (Meta-Native stack).

Primary backend: ``facebook/contriever-msmarco`` served via Transformers on
CPU. Offline backend: deterministic 768-dim L2-normalized synthetic
embeddings built with signed feature hashing, so tests and air-gapped
installs run without ``torch``/``transformers`` or model downloads.
"""

from __future__ import annotations

import hashlib
import re
import warnings
from typing import Sequence

import numpy as np

MODEL_NAME = "facebook/contriever-msmarco"
EMBEDDING_DIM = 768

_TOKEN_RE = re.compile(r"[a-z0-9_]+")
_CHUNK_TEXT_KEYS = ("content", "code", "text", "snippet")

__all__ = [
    "MODEL_NAME",
    "EMBEDDING_DIM",
    "ContrieverEmbeddingModel",
    "offline_embed",
]


def _l2_normalize(matrix: np.ndarray) -> np.ndarray:
    """Row-wise L2 normalization returning a float32 matrix."""
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return (matrix / norms).astype(np.float32)


def _features(text: str) -> list[str]:
    """Word unigrams plus character trigrams for hashed embeddings."""
    lowered = text.lower()
    words = _TOKEN_RE.findall(lowered)
    compact = re.sub(r"\s+", "", lowered)
    trigrams = (
        [compact[i : i + 3] for i in range(len(compact) - 2)]
        if len(compact) >= 3
        else []
    )
    return words + trigrams


def _hash_feature(feature: str) -> tuple[int, float]:
    """Map a feature to a (dimension, sign) pair via SHA-256."""
    digest = hashlib.sha256(feature.encode("utf-8")).digest()
    index = int.from_bytes(digest[:4], "little") % EMBEDDING_DIM
    sign = 1.0 if (digest[4] & 1) == 0 else -1.0
    return index, sign


def offline_embed(texts: Sequence[str]) -> np.ndarray:
    """Deterministic synthetic 768-dim L2-normalized embeddings.

    Signed feature hashing makes lexically similar texts measurably close
    in cosine space, which keeps retrieval tests meaningful without model
    weights.
    """
    matrix = np.zeros((len(texts), EMBEDDING_DIM), dtype=np.float64)
    for row, text in enumerate(texts):
        features = _features(text)
        if not features:
            matrix[row, 0] = 1.0
            continue
        for feature in features:
            index, sign = _hash_feature(feature)
            matrix[row, index] += sign
    return _l2_normalize(matrix)


class ContrieverEmbeddingModel:
    """Contriever dense embedding model with graceful offline fallback.

    Args:
        model_name: Hugging Face model id (default ``MODEL_NAME``).
        device: Torch device for the Transformer backend (default ``cpu``).
        offline: Force offline synthetic embeddings (``True``), force the
            Transformer backend (``False``, raises when unavailable), or
            auto-detect (``None``, default) with fallback on any failure.
    """

    def __init__(
        self,
        model_name: str = MODEL_NAME,
        device: str = "cpu",
        offline: bool | None = None,
    ) -> None:
        self.model_name = model_name
        self.device = device
        self.dim = EMBEDDING_DIM
        self._tokenizer: object | None = None
        self._model: object | None = None
        if offline is True:
            self.offline = True
        elif offline is False:
            self._load_transformer()
            self.offline = False
        else:
            try:
                self._load_transformer()
            except Exception as exc:  # noqa: BLE001 - any load failure
                warnings.warn(
                    f"Contriever weights unavailable ({exc}); "
                    "using offline synthetic embeddings.",
                    RuntimeWarning,
                    stacklevel=2,
                )
                self.offline = True
            else:
                self.offline = False

    def _load_transformer(self) -> None:
        """Load tokenizer and weights onto CPU, raising when impossible."""
        from transformers import AutoModel, AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(self.model_name)
        model = AutoModel.from_pretrained(self.model_name)
        model.to(self.device)
        model.eval()
        self._tokenizer = tokenizer
        self._model = model

    def _encode_transformer(self, texts: list[str]) -> np.ndarray:
        """Mean-pooled, L2-normalized Contriever vectors (CPU, no grad)."""
        import torch

        assert self._tokenizer is not None and self._model is not None
        encoded = self._tokenizer(
            texts,
            padding=True,
            truncation=True,
            max_length=512,
            return_tensors="pt",
        )
        encoded = {key: value.to(self.device) for key, value in encoded.items()}
        with torch.no_grad():
            hidden = self._model(**encoded).last_hidden_state
        mask = encoded["attention_mask"].unsqueeze(-1).to(hidden.dtype)
        pooled = (hidden * mask).sum(dim=1) / mask.sum(dim=1).clamp(min=1.0)
        return _l2_normalize(pooled.float().cpu().numpy())

    def _encode(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        if self.offline:
            return offline_embed(texts)
        return self._encode_transformer(texts)

    def encode_queries(self, texts: list[str]) -> np.ndarray:
        """Embed natural-language queries, shape ``(n, 768)``."""
        if not isinstance(texts, list) or not all(
            isinstance(item, str) for item in texts
        ):
            raise TypeError("texts must be a list of str")
        return self._encode(texts)

    def encode_code_chunks(self, chunks: list[dict]) -> np.ndarray:
        """Embed code chunks, shape ``(n, 768)``.

        Each chunk must be a dict carrying its source under one of the
        ``content`` / ``code`` / ``text`` / ``snippet`` keys.
        """
        if not isinstance(chunks, list) or not all(
            isinstance(item, dict) for item in chunks
        ):
            raise TypeError("chunks must be a list of dict")
        texts: list[str] = []
        for position, chunk in enumerate(chunks):
            for key in _CHUNK_TEXT_KEYS:
                value = chunk.get(key)
                if isinstance(value, str) and value:
                    texts.append(value)
                    break
            else:
                raise ValueError(
                    f"chunk at index {position} has no text under keys "
                    f"{list(_CHUNK_TEXT_KEYS)}"
                )
        return self._encode(texts)
