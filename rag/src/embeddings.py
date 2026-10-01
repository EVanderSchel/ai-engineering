"""Named, swappable embedding functions.

Different embedding models produce vectors of different sizes and meaning,
so each one gets stored in its own Chroma collection (see ingest.py /
retrieval.py collection naming) rather than being mixed together.
"""

import functools

from chromadb.utils import embedding_functions


class CachedDefaultEmbeddingFunction(embedding_functions.DefaultEmbeddingFunction):
    """Chroma's default embedding (all-MiniLM-L6-v2 via ONNX), with the model loaded only once.

    Chroma's DefaultEmbeddingFunction builds a new ONNXMiniLM_L6_V2 on every call, reloading the
    model and tokenizer from disk: about 300 ms per query here, versus about 35 ms when reused.
    Subclassing keeps the name "default", which Chroma records in each collection and checks on
    every get_collection(), so existing collections work unchanged and the vectors are identical.

    Chroma itself bypasses this class when embedding query_texts (it special-cases any
    DefaultEmbeddingFunction), so retrieval.py embeds questions with it directly.
    """

    _model = None

    def __call__(self, input):
        cls = CachedDefaultEmbeddingFunction
        if cls._model is None:
            from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2

            cls._model = ONNXMiniLM_L6_V2()
        return cls._model(input)


MODELS = {
    # Chroma's built-in default: all-MiniLM-L6-v2 via ONNX, 384 dims, no extra
    # download beyond Chroma's own cached model, fast and small.
    "default": CachedDefaultEmbeddingFunction,
    # Larger sentence-transformers model, 768 dims, generally more accurate
    # but slower and downloads its own weights on first use.
    "mpnet": lambda: embedding_functions.SentenceTransformerEmbeddingFunction(model_name="all-mpnet-base-v2"),
    # Small, fast sentence-transformers model tuned for retrieval, 384 dims.
    "bge-small": lambda: embedding_functions.SentenceTransformerEmbeddingFunction(model_name="BAAI/bge-small-en-v1.5"),
}


@functools.cache
def get_embedding_function(name: str):
    """One instance per model for the whole process, so its weights are loaded once, not per query."""
    if name not in MODELS:
        raise ValueError(f"Unknown embedding model '{name}'. Choices: {list(MODELS)}")
    return MODELS[name]()


def collection_name(embedding_model: str) -> str:
    return f"rag_docs__{embedding_model}"
