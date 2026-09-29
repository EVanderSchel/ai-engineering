import json
import logging

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
    assert http.get("/health").json() == {"status": "ok", "generation_enabled": False, "auth_required": False, "version": "dev"}


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
    done = events[-1][1]
    assert done["stop_reason"] == "end_turn"
    assert (done["input_tokens"], done["output_tokens"]) == (100, 5)
    assert done["cost_usd"] == 0.00025  # 100 * $2/M + 5 * $10/M
    assert done["retrieval_ms"] <= done["ttft_ms"] <= done["total_ms"]


def test_stream_reports_midstream_failure_as_error_event(http, fake_client, fake_retrieve):
    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    fake_client.messages.error = anthropic.APIConnectionError(request=request)

    resp = http.post("/ask/stream", json={"question": "q"})

    # The 200 was already sent with the first byte, so the failure has to arrive as an event.
    assert resp.status_code == 200
    events = parse_sse(resp.text)
    assert [name for name, _ in events] == ["sources", "token", "token", "token", "error"]
    assert events[-1][1]["type"] == "APIConnectionError"


def logged_records(caplog) -> list[dict]:
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == "rag.requests"]


def api_error():
    return anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))


def test_ask_logs_one_record_with_usage_and_cost(http, fake_client, fake_retrieve, caplog):
    caplog.set_level(logging.INFO, logger="rag.requests")
    resp = http.post("/ask", json={"question": "What ship was struck?"})

    [record] = logged_records(caplog)
    assert record["request_id"] == resp.headers["X-Request-ID"]
    assert record["status"] == "ok"
    assert record["model"] == "claude-sonnet-5"
    assert record["cost_usd"] == 0.00025
    assert record["n_hits"] == 2
    for key in ("retrieval_ms", "generation_ms", "total_ms"):
        assert record[key] >= 0


def test_logs_never_contain_the_question_text(http, fake_client, fake_retrieve, caplog):
    caplog.set_level(logging.INFO, logger="rag.requests")
    http.post("/ask", json={"question": "my policy number is 12345"})
    http.post("/ask/stream", json={"question": "my policy number is 12345"})

    assert "12345" not in caplog.text
    assert [r["question_chars"] for r in logged_records(caplog)] == [25, 25]


def test_ask_upstream_failure_returns_502_and_is_logged(http, fake_client, fake_retrieve, caplog):
    caplog.set_level(logging.INFO, logger="rag.requests")
    fake_client.messages.error = api_error()

    assert http.post("/ask", json={"question": "q"}).status_code == 502
    [record] = logged_records(caplog)
    assert (record["status"], record["error"]) == ("error", "APIConnectionError")


def test_stream_logs_record_matching_request_id(http, fake_client, fake_retrieve, caplog):
    caplog.set_level(logging.INFO, logger="rag.requests")
    resp = http.post("/ask/stream", json={"question": "q"})

    [record] = logged_records(caplog)
    assert record["request_id"] == resp.headers["X-Request-ID"]
    assert record["status"] == "ok"
    assert record["ttft_ms"] <= record["total_ms"]


def test_stream_failure_is_logged_as_error(http, fake_client, fake_retrieve, caplog):
    caplog.set_level(logging.INFO, logger="rag.requests")
    fake_client.messages.error = api_error()
    http.post("/ask/stream", json={"question": "q"})

    [record] = logged_records(caplog)
    assert (record["status"], record["error"]) == ("error", "APIConnectionError")
    assert "cost_usd" not in record  # the final message never arrived, so usage is unknown


def assert_trace_shape(root, request_id):
    """One trace per request: a retrieval step and a generation step, every observation ended exactly once."""
    assert root.fields["request_id"] == request_id
    assert [c.name for c in root.children] == ["retrieval", "answer"]
    assert all(obs.end_calls == 1 for obs in [root, *root.children])
    retrieval, answer = root.children
    assert retrieval.fields["as_type"] == "retriever"
    assert [s["source"] for s in retrieval.fields["output"]] == ["doc_a.txt", "doc_b.txt"]
    assert answer.fields["as_type"] == "generation"
    assert answer.fields["model"] == "claude-sonnet-5"
    return answer


def test_ask_trace_records_retrieval_and_generation_with_cost(http, fake_client, fake_retrieve, fake_traces):
    resp = http.post("/ask", json={"question": "What ship was struck?"})

    [root] = fake_traces
    answer = assert_trace_shape(root, resp.headers["X-Request-ID"])
    assert answer.fields["usage_details"] == {"input": 100, "output": 5}
    assert answer.fields["cost_details"] == {"total": 0.00025}
    assert root.fields["trace_output"] == "The tanker Trend was struck."


def test_stream_trace_records_time_to_first_token(http, fake_client, fake_retrieve, fake_traces):
    resp = http.post("/ask/stream", json={"question": "What ship was struck?"})

    [root] = fake_traces
    answer = assert_trace_shape(root, resp.headers["X-Request-ID"])
    assert "completion_start_time" in answer.fields
    assert answer.fields["usage_details"] == {"input": 100, "output": 5}


def test_failed_generation_is_marked_as_error_in_trace(http, fake_client, fake_retrieve, fake_traces):
    fake_client.messages.error = api_error()
    http.post("/ask/stream", json={"question": "q"})
    http.post("/ask", json={"question": "q"})

    for root in fake_traces:
        assert root.fields["level"] == "ERROR"
        assert root.children[1].fields["level"] == "ERROR"
        assert all(obs.end_calls == 1 for obs in [root, *root.children])


def test_rejected_request_still_closes_its_trace(http, fake_client, fake_retrieve, fake_traces, monkeypatch):
    def failing_retrieve(question, **kwargs):
        raise RuntimeError("chroma is down")

    monkeypatch.setattr(api, "retrieve", failing_retrieve)
    http_no_raise = type(http)(api.app, raise_server_exceptions=False)
    assert http_no_raise.post("/ask/stream", json={"question": "q"}).status_code == 500

    [root] = fake_traces
    assert root.end_calls == 1
    assert root.children[0].fields["level"] == "ERROR"
