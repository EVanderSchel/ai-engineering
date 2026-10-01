"""Chunk documents from a directory and load them into a Chroma collection (local folder or server)."""

import argparse
import pathlib

import vector_store
from chunking import chunk_text
from embeddings import MODELS, collection_name, get_embedding_function

ROOT = pathlib.Path(__file__).resolve().parent.parent


def build_collection(docs_dir: pathlib.Path, chunk_size: int, overlap: int, embedding_model: str, reset: bool = False):
    """Load a folder's documents into the model's collection, replacing earlier chunks from the same
    files and keeping everything else, so several folders can be ingested side by side."""
    client = vector_store.get_client()
    embedding_fn = get_embedding_function(embedding_model)
    name = collection_name(embedding_model)

    if reset and name in {c.name for c in client.list_collections()}:
        client.delete_collection(name)
    collection = client.get_or_create_collection(name, embedding_function=embedding_fn)

    paths = sorted(docs_dir.glob("*.txt"))
    if paths:
        # Remove this folder's previous chunks first: re-chunking can produce fewer chunks than
        # last time, and stale ones would otherwise linger. Other folders' chunks are untouched.
        collection.delete(where={"source": {"$in": [p.name for p in paths]}})

    ids, documents, metadatas = [], [], []
    for path in paths:
        text = path.read_text(encoding="utf-8")
        chunks = chunk_text(text, chunk_size=chunk_size, overlap=overlap)
        for i, chunk in enumerate(chunks):
            ids.append(f"{path.stem}::chunk{i}")
            documents.append(chunk)
            metadatas.append({"source": path.name, "chunk_index": i})

    if not documents:
        print(f"No .txt files found in {docs_dir}")
        return

    collection.add(ids=ids, documents=documents, metadatas=metadatas)
    print(
        f"Ingested {len(documents)} chunks from {docs_dir} into '{name}' at {vector_store.describe()} "
        f"({collection.count()} chunks in the collection)"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--docs-dir",
        type=pathlib.Path,
        default=ROOT / "data" / "sample_docs",
        help="Directory of .txt files to ingest (default: data/sample_docs)",
    )
    parser.add_argument("--chunk-size", type=int, default=500)
    parser.add_argument("--overlap", type=int, default=50)
    parser.add_argument(
        "--embedding-model",
        choices=list(MODELS),
        default="default",
        help="Which embedding model to use (default: default). Each model gets its own collection.",
    )
    parser.add_argument(
        "--reset", action="store_true", help="Delete the whole collection first, including other folders' chunks"
    )
    args = parser.parse_args()

    build_collection(args.docs_dir, args.chunk_size, args.overlap, args.embedding_model, args.reset)
