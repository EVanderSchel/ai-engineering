"""Where the Chroma database lives: a local folder by default, or a Chroma server if CHROMA_HOST is set.

Either way, embedding happens in this process (see embeddings.py); the server only stores
vectors and runs the similarity search.
"""

import functools
import os
import pathlib

import chromadb

ROOT = pathlib.Path(__file__).resolve().parent.parent
DB_DIR = ROOT / "chroma_db"


def get_client():
    """Return a client for the configured location, reusing it across calls.

    Creating a client isn't free (an HttpClient makes round trips to the server before it's
    usable), and retrieval asks for one on every query. The cache is keyed by location, so
    switching CHROMA_PATH (as eval_gate.py does per corpus) still gets the right database.
    """
    host = os.environ.get("CHROMA_HOST")
    if host:
        return _http_client(host, int(os.environ.get("CHROMA_PORT", "8000")))
    return _local_client(str(_local_path()))


@functools.cache
def _http_client(host: str, port: int):
    return chromadb.HttpClient(host=host, port=port)


@functools.cache
def _local_client(path: str):
    return chromadb.PersistentClient(path=path)


def _local_path() -> pathlib.Path:
    # CHROMA_PATH lets a caller (e.g. eval_gate.py) use a throwaway folder instead of chroma_db/.
    return pathlib.Path(os.environ.get("CHROMA_PATH", DB_DIR))


def describe() -> str:
    """Human-readable location, for log messages."""
    host = os.environ.get("CHROMA_HOST")
    return f"http://{host}:{os.environ.get('CHROMA_PORT', '8000')}" if host else str(_local_path())
