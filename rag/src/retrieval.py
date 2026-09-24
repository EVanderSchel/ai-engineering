"""Retrieval strategies: vector search, BM25 keyword search, hybrid fusion, and cross-encoder reranking."""

import pathlib
import re

import chromadb
from rank_bm25 import BM25Okapi

from embeddings import collection_name, get_embedding_function

ROOT = pathlib.Path(__file__).resolve().parent.parent
DB_DIR = ROOT / "chroma_db"

_bm25_cache: dict[str, tuple] = {}
_reranker = None


def _get_collection(embedding_model: str):
    client = chromadb.PersistentClient(path=str(DB_DIR))
    embedding_fn = get_embedding_function(embedding_model)
    return client.get_collection(collection_name(embedding_model), embedding_function=embedding_fn)


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _load_bm25_index(embedding_model: str):
    if embedding_model not in _bm25_cache:
        collection = _get_collection(embedding_model)
        result = collection.get(include=["documents", "metadatas"])
        bm25 = BM25Okapi([_tokenize(doc) for doc in result["documents"]])
        _bm25_cache[embedding_model] = (bm25, result["ids"], result["documents"], result["metadatas"])
    return _bm25_cache[embedding_model]


def vector_retrieve(question: str, k: int, embedding_model: str = "default") -> list[dict]:
    collection = _get_collection(embedding_model)
    results = collection.query(query_texts=[question], n_results=k)
    return [
        {"id": doc_id, "text": doc, "source": meta["source"], "distance": distance}
        for doc_id, doc, meta, distance in zip(
            results["ids"][0], results["documents"][0], results["metadatas"][0], results["distances"][0]
        )
    ]


def bm25_retrieve(question: str, k: int, embedding_model: str = "default") -> list[dict]:
    bm25, ids, documents, metadatas = _load_bm25_index(embedding_model)
    scores = bm25.get_scores(_tokenize(question))
    ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:k]
    return [
        {"id": ids[i], "text": documents[i], "source": metadatas[i]["source"], "score": scores[i]}
        for i in ranked
    ]


def hybrid_retrieve(
    question: str, k: int, embedding_model: str = "default", candidates: int = 20, rrf_k: int = 60
) -> list[dict]:
    """Combine vector and BM25 rankings via Reciprocal Rank Fusion.

    RRF scores each result by 1/(rrf_k + rank) in each ranking and sums across
    rankings, so a chunk that ranks well on both keyword and semantic match
    rises to the top without needing to normalize very different score scales.
    """
    vector_hits = vector_retrieve(question, candidates, embedding_model)
    bm25_hits = bm25_retrieve(question, candidates, embedding_model)

    fused_scores: dict[str, float] = {}
    lookup: dict[str, dict] = {}
    for rank, hit in enumerate(vector_hits, start=1):
        fused_scores[hit["id"]] = fused_scores.get(hit["id"], 0) + 1 / (rrf_k + rank)
        lookup[hit["id"]] = hit
    for rank, hit in enumerate(bm25_hits, start=1):
        fused_scores[hit["id"]] = fused_scores.get(hit["id"], 0) + 1 / (rrf_k + rank)
        lookup.setdefault(hit["id"], hit)

    ranked_ids = sorted(fused_scores, key=fused_scores.get, reverse=True)[:k]
    return [dict(lookup[doc_id], score=fused_scores[doc_id]) for doc_id in ranked_ids]


def _get_reranker():
    global _reranker
    if _reranker is None:
        from sentence_transformers import CrossEncoder

        _reranker = CrossEncoder("cross-encoder/ms-marco-MiniLM-L-6-v2")
    return _reranker


def rerank(question: str, hits: list[dict], k: int) -> list[dict]:
    """Re-score candidates with a cross-encoder that reads (question, chunk) jointly.

    Cross-encoders are far more accurate than embedding similarity but too slow
    to run over a whole corpus, so they're used only to re-order a small
    candidate set already narrowed down by vector or hybrid search.
    """
    if not hits:
        return hits
    model = _get_reranker()
    scores = model.predict([(question, hit["text"]) for hit in hits])
    ranked = sorted(zip(hits, scores), key=lambda pair: pair[1], reverse=True)[:k]
    return [dict(hit, rerank_score=float(score)) for hit, score in ranked]


def retrieve(
    question: str,
    k: int = 3,
    embedding_model: str = "default",
    hybrid: bool = False,
    use_reranker: bool = False,
    rerank_candidates: int = 10,
) -> list[dict]:
    if use_reranker:
        candidate_fn = hybrid_retrieve if hybrid else vector_retrieve
        candidates = candidate_fn(question, rerank_candidates, embedding_model)
        return rerank(question, candidates, k)

    if hybrid:
        return hybrid_retrieve(question, k, embedding_model)

    return vector_retrieve(question, k, embedding_model)
