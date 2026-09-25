"""Shared test fixtures: a fake Claude client and a fake retriever, so tests are fast, free, and deterministic."""

from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

import api

# Scores are numpy floats on purpose: that's what BM25 returns, and JSON can't encode them.
FAKE_HITS = [
    {"id": "doc_a::chunk0", "text": "The tanker Trend was struck on September 18.", "source": "doc_a.txt", "score": np.float64(0.9)},
    {"id": "doc_b::chunk3", "text": "Shipping insurance rates rose.", "source": "doc_b.txt", "score": np.float64(0.4)},
]


def _final_message(text: str) -> SimpleNamespace:
    """Mimic the fields of an anthropic Message that our code reads."""
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason="end_turn",
        usage=SimpleNamespace(input_tokens=100, output_tokens=len(text.split())),
    )


class FakeStream:
    """Stands in for the context manager returned by client.messages.stream()."""

    def __init__(self, chunks: list[str], error: Exception | None):
        self._chunks = chunks
        self._error = error

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def text_stream(self):
        for chunk in self._chunks:
            yield chunk
        if self._error:
            raise self._error  # fail partway through, like a dropped connection

    def get_final_message(self):
        return _final_message("".join(self._chunks))


class FakeMessages:
    def __init__(self, chunks: list[str]):
        self.chunks = chunks
        self.error: Exception | None = None
        self.calls: list[dict] = []  # every request's kwargs, so tests can inspect what was sent

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _final_message("".join(self.chunks))

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        return FakeStream(self.chunks, self.error)


class FakeClient:
    def __init__(self, chunks: list[str]):
        self.messages = FakeMessages(chunks)


@pytest.fixture
def fake_client(monkeypatch):
    client = FakeClient(["The tanker ", "Trend", " was struck."])
    monkeypatch.setattr(api, "client", client)
    return client


@pytest.fixture
def fake_retrieve(monkeypatch):
    """Replace real retrieval (which needs a built Chroma index) and record its calls."""
    calls = []

    def retrieve(question, **kwargs):
        calls.append({"question": question, **kwargs})
        return FAKE_HITS

    monkeypatch.setattr(api, "retrieve", retrieve)
    return calls


@pytest.fixture
def http():
    return TestClient(api.app)
