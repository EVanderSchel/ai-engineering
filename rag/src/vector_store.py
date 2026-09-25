"""Where the Chroma database lives: a local folder by default, or a Chroma server if CHROMA_HOST is set.

Either way, embedding happens in this process (see embeddings.py); the server only stores
vectors and runs the similarity search.
"""

import os
import pathlib

import chromadb

ROOT = pathlib.Path(__file__).resolve().parent.parent
DB_DIR = ROOT / "chroma_db"


def get_client():
    host = os.environ.get("CHROMA_HOST")
    if host:
        return chromadb.HttpClient(host=host, port=int(os.environ.get("CHROMA_PORT", "8000")))
    return chromadb.PersistentClient(path=str(DB_DIR))


def describe() -> str:
    """Human-readable location, for log messages."""
    host = os.environ.get("CHROMA_HOST")
    return f"http://{host}:{os.environ.get('CHROMA_PORT', '8000')}" if host else str(DB_DIR)
