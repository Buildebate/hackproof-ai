"""Step 4.3b.2 (R5): hybrid retrieval ranking (BM25 + RRF + downrank).

Hermetic vectors for the stdlib BM25 channel, reciprocal-rank fusion,
test/doc downranking, the widened candidate window, and an end-to-end
localize_issue pass over a fixture repo (offline embeddings, no torch).
"""

from __future__ import annotations

from pathlib import Path

from hackproof import cli
from hackproof.retrieval.bm25 import BM25Index, tokenize
from hackproof.retrieval.fusion import downrank_test_and_doc, is_test_or_doc_path, rrf_fuse


def test_tokenize_splits_code_identifiers() -> None:
    assert tokenize("useCssVars + __SSR__!") == ["usecssvars", "ssr"]
    assert tokenize("TestReportSerialization") == ["testreportserialization"]
    assert tokenize("") == []


def test_bm25_ranks_rare_term_match_first() -> None:
    docs = [
        "def render(value): return value",
        "def stringify_style(styles): hyphenate keyframes",
        "def render_list(items): return items",
    ]
    index = BM25Index(docs)
    ranked = index.top("hyphenate keyframes return", n=3)
    assert [doc_id for doc_id, _ in ranked][0] == 1
    assert ranked[0][1] > ranked[1][1] > 0


def test_bm25_deterministic_and_empty_safe() -> None:
    index = BM25Index(["alpha beta", "beta gamma"])
    first = index.top("alpha", n=2)
    second = index.top("alpha", n=2)
    assert first == second
    assert index.top("", n=2) == []
    assert BM25Index([]).top("alpha", n=2) == []


def test_rrf_fuse_prefers_consensus() -> None:
    fused = rrf_fuse([["a", "b", "c"], ["b", "a", "d"]])
    order = [doc_id for doc_id, _ in fused]
    assert order[:2] == ["a", "b"]
    assert set(order) == {"a", "b", "c", "d"}
    scores = dict(fused)
    assert scores["a"] > scores["c"]
    assert scores["b"] > scores["d"]


def test_is_test_or_doc_path_matrix() -> None:
    assert is_test_or_doc_path("testing/io/test_saferepr.py")
    assert is_test_or_doc_path("tests/test_utils.py")
    assert is_test_or_doc_path("packages/x/__tests__/a.spec.ts")
    assert is_test_or_doc_path("doc/en/example/xfail_demo.py")
    assert is_test_or_doc_path("bench/skip.py")
    assert is_test_or_doc_path("conftest.py")
    assert not is_test_or_doc_path("src/_pytest/skipping.py")
    assert not is_test_or_doc_path("src/_pytest/reports.py")
    assert not is_test_or_doc_path("packages/runtime-core/src/helpers/useCssVars.ts")
    assert not is_test_or_doc_path("requests/models.py")


def test_downrank_sinks_tests_below_sources_on_ties() -> None:
    ranked = [("testing/test_x.py", 0.9), ("src/mod.py", 0.9)]
    out = downrank_test_and_doc(ranked)
    assert [path for path, _ in out] == ["src/mod.py", "testing/test_x.py"]
    assert out[0][1] == 0.9
    assert out[1][1] < 0.9


def test_collect_code_chunks_window_covers_large_tree(tmp_path: Path) -> None:
    for i in range(205):
        (tmp_path / f"mod_{i:04d}.py").write_text(f"VALUE_{i} = {i}\n", encoding="utf-8")
    chunks = cli.collect_code_chunks(tmp_path, "python")
    assert len(chunks) == 205


def test_localize_issue_prefers_source_over_tests(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "reports.py").write_text(
        "def serialize_chained_exception(exc):\n    return repr(exc.__cause__)\n",
        encoding="utf-8",
    )
    (tmp_path / "testing").mkdir()
    (tmp_path / "testing" / "test_reports.py").write_text(
        "def test_serialize_chained_exception():\n    assert True\n",
        encoding="utf-8",
    )
    snippets, files, _ = cli.localize_issue(
        tmp_path, "chained exceptions serialization TestReport", "python"
    )
    assert files[0] == "src/reports.py"
    assert len(snippets) == len(files) == 2
