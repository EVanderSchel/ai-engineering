"""Chunk documents from data/sample_docs and load them into a local Chroma collection."""

import argparse
import pathlib

import chromadb
from chromadb.utils import embedding_functions

from chunking import chunk_text

ROOT = pathlib.Path(__file__).resolve().parent.parent
DB_DIR = ROOT / "chroma_db"
COLLECTION_NAME = "rag_docs"


def build_collection(docs_dir: pathlib.Path, chunk_size: int, overlap: int):
    client = chromadb.PersistentClient(path=str(DB_DIR))
    embedding_fn = embedding_functions.DefaultEmbeddingFunction()

    # Start fresh each run so re-ingesting doesn't duplicate chunks.
    existing = {c.name for c in client.list_collections()}
    if COLLECTION_NAME in existing:
        client.delete_collection(COLLECTION_NAME)
    collection = client.create_collection(COLLECTION_NAME, embedding_function=embedding_fn)

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
    print(f"Ingested {len(documents)} chunks from {docs_dir} into '{COLLECTION_NAME}' at {DB_DIR}")


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
    args = parser.parse_args()

    build_collection(args.docs_dir, args.chunk_size, args.overlap)
