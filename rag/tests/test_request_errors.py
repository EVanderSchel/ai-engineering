"""Failure modes found by auditing the request path, one test (or more) per fix."""

import json
import logging
from types import SimpleNamespace

import chromadb.errors
import httpx
from fastapi.testclient import TestClient

import api
import retrieval
import vector_store
from query import ANSWER_MAX_TOKENS, answer_text


def logged_records(caplog) -> list[dict]:
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == "rag.requests"]


# --- 1. Answer text, length limit, refusals -----------------------------------------------------

def test_answer_text_skips_non_text_blocks():
    thinking = SimpleNamespace(type="thinking", thinking="")
    message = SimpleNamespace(content=[thinking, SimpleNamespace(type="text", text="Hello "),
                                       SimpleNamespace(type="text", text="world")])
    assert answer_text(message) == "Hello world"


def test_ask_works_when_a_thinking_block_comes_first(http, fake_client, fake_retrieve):
    fake_client.messages.leading_blocks = [SimpleNamespace(type="thinking", thinking="")]
    resp = http.post("/ask", json={"question": "q"})
    assert resp.status_code == 200
    assert resp.json()["answer"] == "The tanker Trend was struck."


def test_ask_reports_stop_reason_so_cut_off_answers_are_visible(http, fake_client, fake_retrieve):
    assert http.post("/ask", json={"question": "q"}).json()["stop_reason"] == "end_turn"
    fake_client.messages.stop_reason = "max_tokens"
    assert http.post("/ask", json={"question": "q"}).json()["stop_reason"] == "max_tokens"


def test_answer_length_limit_leaves_room_for_long_answers(http, fake_client, fake_retrieve):
    http.post("/ask", json={"question": "q"})
    assert fake_client.messages.calls[0]["max_tokens"] == ANSWER_MAX_TOKENS >= 1024


def test_refusal_is_a_clear_422_and_logged_as_refused(http, fake_client, fake_retrieve, caplog):
    caplog.set_level(logging.INFO, logger="rag.requests")
    fake_client.messages.stop_reason = "refusal"

    resp = http.post("/ask", json={"question": "q"})

    assert resp.status_code == 422
    assert resp.json()["detail"] == "Claude declined to answer this question."
    [record] = logged_records(caplog)
    assert record["status"] == "refused"


def test_stream_refusal_is_reported_in_done_event_and_log(http, fake_client, fake_retrieve, caplog):
    caplog.set_level(logging.INFO, logger="rag.requests")
    fake_client.messages.stop_reason = "refusal"

    body = http.post("/ask/stream", json={"question": "q"}).text

    assert '"stop_reason": "refusal"' in body
    [record] = logged_records(caplog)
    assert record["status"] == "refused"


# --- 2. Search index unavailable or empty --------------------------------------------------------

def test_chroma_unreachable_is_503_with_retry_after(http, fake_client, monkeypatch):
    def unreachable(question, **kwargs):
        raise vector_store.VectorStoreUnavailable("Can't reach the Chroma server at chroma:8000")

    monkeypatch.setattr(api, "retrieve", unreachable)
    for path in ("/ask", "/ask/stream"):
        resp = http.post(path, json={"question": "q"})
        assert resp.status_code == 503
        assert resp.headers["Retry-After"] == "10"


def test_connection_dropped_mid_query_is_503(http, fake_client, monkeypatch):
    def dropped(question, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(api, "retrieve", dropped)
    assert http.post("/ask", json={"question": "q"}).status_code == 503


def test_get_client_wraps_connection_failures(monkeypatch):
    monkeypatch.setenv("CHROMA_HOST", "127.0.0.1")
    monkeypatch.setenv("CHROMA_PORT", "1")  # nothing listens on port 1
    try:
        vector_store.get_client()
    except vector_store.VectorStoreUnavailable as e:
        assert "127.0.0.1:1" in str(e)
    else:
        raise AssertionError("expected VectorStoreUnavailable")


def test_keyword_search_on_an_empty_index_returns_nothing(monkeypatch):
    empty = SimpleNamespace(get=lambda include: {"ids": [], "documents": [], "metadatas": []})
    monkeypatch.setattr(retrieval, "_get_collection", lambda model: empty)
    retrieval.clear_caches()
    try:
        assert retrieval.bm25_retrieve("anything", k=3) == []
    finally:
        retrieval.clear_caches()


# --- 3. Claude call timeout ----------------------------------------------------------------------

def test_claude_client_has_a_short_timeout(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    assert api.make_client().timeout == api.CLAUDE_TIMEOUT_SECONDS == 60.0


# --- 4. Every request is logged exactly once ------------------------------------------------------

def test_unexpected_error_is_logged_once_as_error(fake_client, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="rag.requests")

    def broken(question, **kwargs):
        raise RuntimeError("something nobody planned for")

    monkeypatch.setattr(api, "retrieve", broken)
    http = TestClient(api.app, raise_server_exceptions=False)
    for path in ("/ask", "/ask/stream"):
        assert http.post(path, json={"question": "q"}).status_code == 500

    records = logged_records(caplog)
    assert [(r["endpoint"], r["status"], r["error"]) for r in records] == [
        ("/ask", "error", "RuntimeError"),
        ("/ask/stream", "error", "RuntimeError"),
    ]


def test_deliberate_http_errors_are_logged_too(http, fake_client, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="rag.requests")

    def missing(question, **kwargs):
        raise chromadb.errors.NotFoundError("no index")

    monkeypatch.setattr(api, "retrieve", missing)
    http.post("/ask", json={"question": "q", "embedding_model": "mpnet"})

    [record] = logged_records(caplog)
    assert (record["status"], record["error"]) == ("error", "HTTP 422")


def test_successful_request_is_still_logged_exactly_once(http, fake_client, fake_retrieve, caplog):
    caplog.set_level(logging.INFO, logger="rag.requests")
    http.post("/ask", json={"question": "q"})
    assert [r["status"] for r in logged_records(caplog)] == ["ok"]
