import embeddings
import retrieval


class FakeCollection:
    def __init__(self):
        self.query_kwargs = None

    def query(self, **kwargs):
        self.query_kwargs = kwargs
        return {"ids": [["a"]], "documents": [["text"]], "metadatas": [[{"source": "a.txt"}]], "distances": [[0.1]]}


class FakeEmbedder:
    def embed_query(self, input):
        return [[0.1, 0.2, 0.3]]


def test_vector_search_sends_a_vector_not_text(monkeypatch):
    """Passing query_texts= makes Chroma rebuild its default model on every query (~230 ms).
    Retrieval must embed the question itself with the cached model and pass the vector."""
    collection = FakeCollection()
    monkeypatch.setattr(retrieval, "_get_collection", lambda model: collection)
    monkeypatch.setattr(retrieval, "get_embedding_function", lambda model: FakeEmbedder())

    hits = retrieval.vector_retrieve("q", k=1)

    assert "query_texts" not in collection.query_kwargs
    assert collection.query_kwargs["query_embeddings"] == [[0.1, 0.2, 0.3]]
    assert hits == [{"id": "a", "text": "text", "source": "a.txt", "distance": 0.1}]


def test_embedding_function_is_created_once_per_model():
    assert embeddings.get_embedding_function("default") is embeddings.get_embedding_function("default")
