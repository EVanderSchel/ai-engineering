"""Chunk documents from a directory and load them into a local Chroma collection."""

import argparse
import pathlib

import chromadb

from chunking import chunk_text
from embeddings import collection_name, get_embedding_function, MODELS

ROOT = pathlib.Path(__file__).resolve().parent.parent
DB_DIR = ROOT / "chroma_db"


def build_collection(docs_dir: pathlib.Path, chunk_size: int, overlap: int, embedding_model: str):
    client = chromadb.PersistentClient(path=str(DB_DIR))
    embedding_fn = get_embedding_function(embedding_model)
    name = collection_name(embedding_model)

    # Start fresh each run so re-ingesting doesn't duplicate chunks.
    existing = {c.name for c in client.list_collections()}
    if name in existing:
        client.delete_collection(name)
    collection = client.create_collection(name, embedding_function=embedding_fn)

    ids, documents, metadatas = [], [], []
    for path in sorted(docs_dir.glob("*.txt")):
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
    print(f"Ingested {len(documents)} chunks from {docs_dir} into '{name}' at {DB_DIR}")


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
    args = parser.parse_args()

    build_collection(args.docs_dir, args.chunk_size, args.overlap, args.embedding_model)
