import pytest
from chromadb.api.types import EmbeddingFunction

import ingest
import vector_store


class TinyEmbedder(EmbeddingFunction):
    """Deterministic 3-number 'embedding', so the test needs no model download."""

    def __init__(self):
        pass

    def __call__(self, input):
        return [[float(len(text)), float(text.count(" ")), 1.0] for text in input]

    @staticmethod
    def name():
        return "tiny-test-embedder"

    # Chroma stores each collection's embedding-function config and will require these.
    def get_config(self):
        return {}

    @staticmethod
    def build_from_config(config):
        return TinyEmbedder()


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A throwaway local Chroma database, with ingest using the tiny embedder."""
    monkeypatch.delenv("CHROMA_HOST", raising=False)
    monkeypatch.setenv("CHROMA_PATH", str(tmp_path / "db"))
    monkeypatch.setattr(ingest, "get_embedding_function", lambda model: TinyEmbedder())
    return lambda: vector_store.get_client().get_collection("rag_docs__default")


def make_corpus(folder, files):
    folder.mkdir()
    for name, text in files.items():
        (folder / name).write_text(text, encoding="utf-8")
    return folder


def sources(collection):
    return sorted({m["source"] for m in collection.get(include=["metadatas"])["metadatas"]})


def test_ingesting_a_second_folder_keeps_the_first(tmp_path, store):
    a = make_corpus(tmp_path / "a", {"one.txt": "Alpha text.", "two.txt": "Beta text."})
    b = make_corpus(tmp_path / "b", {"three.txt": "Gamma text."})

    ingest.build_collection(a, 500, 50, "default")
    ingest.build_collection(b, 500, 50, "default")

    assert sources(store()) == ["one.txt", "three.txt", "two.txt"]


def test_reingesting_a_folder_replaces_its_chunks_instead_of_duplicating(tmp_path, store):
    a = make_corpus(tmp_path / "a", {"one.txt": "Alpha text."})
    ingest.build_collection(a, 500, 50, "default")
    ingest.build_collection(a, 500, 50, "default")
    assert store().count() == 1


def test_reset_removes_other_folders(tmp_path, store):
    a = make_corpus(tmp_path / "a", {"one.txt": "Alpha text."})
    b = make_corpus(tmp_path / "b", {"three.txt": "Gamma text."})
    ingest.build_collection(a, 500, 50, "default")
    ingest.build_collection(b, 500, 50, "default", reset=True)
    assert sources(store()) == ["three.txt"]
