"""Step 1.2: dense semantic retrieval (Contriever + Faiss index)."""

import time

import numpy as np
import pytest

from hackproof.retrieval import ContrieverEmbeddingModel, FaissCodeIndexer
from hackproof.retrieval.contriever import EMBEDDING_DIM

try:
    import faiss  # noqa: F401

    _FAISS_INSTALLED = True
except ImportError:
    _FAISS_INSTALLED = False

BACKENDS = ["numpy"] + (["faiss"] if _FAISS_INSTALLED else [])

SNIPPETS = [
    {
        "path": "auth.py",
        "content": (
            "def authenticate_user(username, password): "
            "verify password hash and return session token"
        ),
    },
    {
        "path": "db.py",
        "content": (
            "def connect_database(host, port): "
            "open postgres connection pool with retry"
        ),
    },
    {
        "path": "email.py",
        "content": (
            "def send_email(recipient, subject, body): "
            "dispatch message via smtp relay"
        ),
    },
    {
        "path": "logging_setup.py",
        "content": (
            "def write_log_entry(level, message): "
            "append structured json record to log file"
        ),
    },
    {
        "path": "cache.py",
        "content": (
            "def get_cached_value(key): read entry from redis cache with ttl"
        ),
    },
    {
        "path": "http_client.py",
        "content": (
            "def fetch_http_resource(url): "
            "perform get request and parse json response"
        ),
    },
]


@pytest.fixture(scope="module")
def model() -> ContrieverEmbeddingModel:
    return ContrieverEmbeddingModel(offline=True)


@pytest.fixture(params=BACKENDS)
def backend_name(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def test_offline_query_shape_dtype_and_normalization(
    model: ContrieverEmbeddingModel,
) -> None:
    vectors = model.encode_queries(["authenticate user", "open database"])
    assert vectors.shape == (2, EMBEDDING_DIM)
    assert vectors.dtype == np.float32
    norms = np.linalg.norm(vectors, axis=1)
    assert np.allclose(norms, 1.0, atol=1e-5)


def test_offline_embeddings_are_deterministic(
    model: ContrieverEmbeddingModel,
) -> None:
    first = model.encode_queries(["stable text"])
    second = model.encode_queries(["stable text"])
    assert np.array_equal(first, second)


def test_distinct_texts_yield_distinct_vectors(
    model: ContrieverEmbeddingModel,
) -> None:
    vectors = model.encode_queries(["alpha beta gamma", "delta epsilon zeta"])
    assert not np.array_equal(vectors[0], vectors[1])


def test_encode_code_chunks_shape_and_normalization(
    model: ContrieverEmbeddingModel,
) -> None:
    vectors = model.encode_code_chunks(SNIPPETS)
    assert vectors.shape == (len(SNIPPETS), EMBEDDING_DIM)
    assert vectors.dtype == np.float32
    assert np.allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=1e-5)


def test_encode_code_chunks_accepts_alias_keys(
    model: ContrieverEmbeddingModel,
) -> None:
    vectors = model.encode_code_chunks(
        [{"code": "x = 1"}, {"text": "y = 2"}, {"snippet": "z = 3"}]
    )
    assert vectors.shape == (3, EMBEDDING_DIM)


def test_encode_code_chunks_rejects_textless_chunk(
    model: ContrieverEmbeddingModel,
) -> None:
    with pytest.raises(ValueError, match="no text"):
        model.encode_code_chunks([{"path": "empty.py"}])


def test_index_self_retrieval_is_top1(
    model: ContrieverEmbeddingModel, backend_name: str
) -> None:
    index = FaissCodeIndexer(backend=backend_name)
    vectors = model.encode_code_chunks(SNIPPETS)
    index.add_chunks(SNIPPETS, vectors)
    assert len(index) == len(SNIPPETS)
    for position, chunk in enumerate(SNIPPETS):
        [(best_chunk, best_score)] = index.search(vectors[position], top_k=1)
        assert best_chunk["path"] == chunk["path"]
        assert best_score == pytest.approx(1.0, abs=1e-5)


def test_search_top_k_ordering_and_limit(
    model: ContrieverEmbeddingModel, backend_name: str
) -> None:
    index = FaissCodeIndexer(backend=backend_name)
    index.add_chunks(SNIPPETS, model.encode_code_chunks(SNIPPETS))
    query = model.encode_queries(["database connection"])[0]
    results = index.search(query, top_k=3)
    assert len(results) == 3
    scores = [score for _, score in results]
    assert scores == sorted(scores, reverse=True)


def test_end_to_end_code_snippet_localization(
    model: ContrieverEmbeddingModel, backend_name: str
) -> None:
    index = FaissCodeIndexer(backend=backend_name)
    index.add_chunks(SNIPPETS, model.encode_code_chunks(SNIPPETS))
    query = model.encode_queries(["authenticate user password session"])[0]
    [(best_chunk, best_score)] = index.search(query, top_k=1)
    assert best_chunk["path"] == "auth.py"
    assert best_score > 0.3


def test_search_latency_within_15ms(
    model: ContrieverEmbeddingModel, backend_name: str
) -> None:
    corpus = [
        {"path": f"mod_{i}.py", "content": f"def handler_{i}(x): return x + {i}"}
        for i in range(500)
    ]
    index = FaissCodeIndexer(backend=backend_name)
    index.add_chunks(corpus, model.encode_code_chunks(corpus))
    query = model.encode_queries(["handler function return"])[0]
    index.search(query, top_k=5)  # warm-up
    latencies: list[float] = []
    for _ in range(20):
        started = time.perf_counter()
        index.search(query, top_k=5)
        latencies.append(time.perf_counter() - started)
    mean_latency = sum(latencies) / len(latencies)
    assert mean_latency <= 0.015


def test_search_empty_index_returns_empty_list(backend_name: str) -> None:
    index = FaissCodeIndexer(backend=backend_name)
    assert index.search(np.zeros(EMBEDDING_DIM, dtype=np.float32)) == []


def test_add_chunks_validates_shapes(backend_name: str) -> None:
    index = FaissCodeIndexer(backend=backend_name)
    with pytest.raises(ValueError, match=r"\(n, 768\)"):
        index.add_chunks(SNIPPETS[:2], np.zeros((2, 64), dtype=np.float32))
    with pytest.raises(ValueError, match="equal length"):
        index.add_chunks(SNIPPETS[:2], np.zeros((3, 768), dtype=np.float32))
