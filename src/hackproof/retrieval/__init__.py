"""Dense semantic retrieval: Contriever embeddings + Faiss code index."""

from hackproof.retrieval.contriever import ContrieverEmbeddingModel
from hackproof.retrieval.faiss_index import FaissCodeIndexer

__all__ = ["ContrieverEmbeddingModel", "FaissCodeIndexer"]
