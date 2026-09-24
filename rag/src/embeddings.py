"""Named, swappable embedding functions.

Different embedding models produce vectors of different sizes and meaning,
so each one gets stored in its own Chroma collection (see ingest.py /
retrieval.py collection naming) rather than being mixed together.
"""

from chromadb.utils import embedding_functions

MODELS = {
    # Chroma's built-in default: all-MiniLM-L6-v2 via ONNX, 384 dims, no extra
    # download beyond Chroma's own cached model, fast and small.
    "default": embedding_functions.DefaultEmbeddingFunction,
    # Larger sentence-transformers model, 768 dims, generally more accurate
    # but slower and downloads its own weights on first use.
    "mpnet": lambda: embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name="all-mpnet-base-v2"
    ),
    # Small, fast sentence-transformers model tuned for retrieval, 384 dims.
    "bge-small": lambda: embedding_functions.SentenceTransformerEmbeddingFunction(
        model_name="BAAI/bge-small-en-v1.5"
    ),
}


def get_embedding_function(name: str):
    if name not in MODELS:
        raise ValueError(f"Unknown embedding model '{name}'. Choices: {list(MODELS)}")
    return MODELS[name]()


def collection_name(embedding_model: str) -> str:
    return f"rag_docs__{embedding_model}"
