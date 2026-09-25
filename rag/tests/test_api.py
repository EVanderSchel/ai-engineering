import json

import anthropic
import httpx2

import api


def parse_sse(body: str) -> list[tuple[str, object]]:
    """Split a Server-Sent Events body into (event name, decoded data) pairs."""
    events = []
    for block in body.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines())
        events.append((fields["event"], json.loads(fields["data"])))
    return events


def test_health_reports_generation_disabled_without_key(http, monkeypatch):
    monkeypatch.setattr(api, "client", None)
    assert http.get("/health").json() == {"status": "ok", "generation_enabled": False}


def test_ask_returns_answer_and_sources(http, fake_client, fake_retrieve):
    resp = http.post("/ask", json={"question": "What ship was struck?"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == "The tanker Trend was struck."
    assert [s["source"] for s in body["sources"]] == ["doc_a.txt", "doc_b.txt"]
    assert "score" not in body["sources"][0]


def test_ask_sends_retrieved_chunks_to_claude(http, fake_client, fake_retrieve):
    http.post("/ask", json={"question": "What ship was struck?"})

    prompt = fake_client.messages.calls[0]["messages"][0]["content"]
    assert "[doc_a.txt] The tanker Trend was struck on September 18." in prompt
    assert "Question: What ship was struck?" in prompt


def test_request_options_are_passed_to_retrieval(http, fake_client, fake_retrieve):
    http.post("/ask", json={"question": "q", "k": 5, "hybrid": True, "rerank": True})

    assert fake_retrieve[0] == {
        "question": "q",
        "k": 5,
        "embedding_model": "default",
        "hybrid": True,
        "use_reranker": True,
    }


def test_ask_without_api_key_returns_503(http, fake_retrieve, monkeypatch):
    monkeypatch.setattr(api, "client", None)
    assert http.post("/ask", json={"question": "q"}).status_code == 503


def test_empty_question_is_rejected(http, fake_client, fake_retrieve):
    assert http.post("/ask/stream", json={"question": ""}).status_code == 422
    assert fake_retrieve == []


def test_unknown_embedding_model_is_rejected_before_retrieval(http, fake_client, fake_retrieve):
    resp = http.post("/ask/stream", json={"question": "q", "embedding_model": "nope"})

    assert resp.status_code == 422
    assert fake_retrieve == []
    assert fake_client.messages.calls == []


def test_stream_sends_sources_then_tokens_then_done(http, fake_client, fake_retrieve):
    resp = http.post("/ask/stream", json={"question": "What ship was struck?"})

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(resp.text)
    names = [name for name, _ in events]

    assert names == ["sources", "token", "token", "token", "done"]
    assert [s["id"] for s in events[0][1]] == ["doc_a::chunk0", "doc_b::chunk3"]
    assert "".join(data for name, data in events if name == "token") == "The tanker Trend was struck."
    assert events[-1][1] == {"stop_reason": "end_turn", "input_tokens": 100, "output_tokens": 5}


def test_stream_reports_midstream_failure_as_error_event(http, fake_client, fake_retrieve):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    fake_client.messages.error = anthropic.APIConnectionError(request=request)

    resp = http.post("/ask/stream", json={"question": "q"})

    # The 200 was already sent with the first byte, so the failure has to arrive as an event.
    assert resp.status_code == 200
    events = parse_sse(resp.text)
    assert [name for name, _ in events] == ["sources", "token", "token", "token", "error"]
    assert events[-1][1]["type"] == "APIConnectionError"
