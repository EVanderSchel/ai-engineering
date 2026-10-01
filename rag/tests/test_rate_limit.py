import rate_limit


def test_allows_up_to_the_limit_then_rejects(monkeypatch):
    monkeypatch.setenv("RAG_RATE_LIMIT_PER_MINUTE", "3")
    assert [rate_limit.check("a", now=t) for t in (0, 1, 2)] == [None, None, None]
    assert rate_limit.check("a", now=3) == 57  # seconds until the request at t=0 leaves the window


def test_window_rolls_forward(monkeypatch):
    monkeypatch.setenv("RAG_RATE_LIMIT_PER_MINUTE", "2")
    rate_limit.check("a", now=0)
    rate_limit.check("a", now=30)
    assert rate_limit.check("a", now=59) is not None
    assert rate_limit.check("a", now=60) is None  # the t=0 request has expired


def test_callers_are_counted_separately(monkeypatch):
    monkeypatch.setenv("RAG_RATE_LIMIT_PER_MINUTE", "1")
    assert rate_limit.check("a", now=0) is None
    assert rate_limit.check("b", now=0) is None
    assert rate_limit.check("a", now=1) is not None


def test_zero_disables_the_limit(monkeypatch):
    monkeypatch.setenv("RAG_RATE_LIMIT_PER_MINUTE", "0")
    assert all(rate_limit.check("a", now=0) is None for _ in range(100))


def test_caller_id_is_the_key_when_authenticated_otherwise_the_ip():
    assert rate_limit.caller_id(True, "1.2.3.4") == "api-key"
    assert rate_limit.caller_id(False, "1.2.3.4") == "ip:1.2.3.4"


def test_without_auth_made_up_keys_cannot_dodge_the_limit(http, fake_client, fake_retrieve, monkeypatch):
    """With RAG_API_KEY unset the header isn't checked, so a new value per request must not reset the count."""
    monkeypatch.setenv("RAG_RATE_LIMIT_PER_MINUTE", "1")
    first = http.post("/ask", json={"question": "q"}, headers={"X-API-Key": "made-up-1"})
    second = http.post("/ask", json={"question": "q"}, headers={"X-API-Key": "made-up-2"})
    assert (first.status_code, second.status_code) == (200, 429)


def test_api_returns_429_with_retry_after_and_never_reaches_claude(http, fake_client, fake_retrieve, monkeypatch):
    monkeypatch.setenv("RAG_RATE_LIMIT_PER_MINUTE", "2")
    codes = [http.post("/ask", json={"question": "q"}).status_code for _ in range(3)]
    assert codes == [200, 200, 429]

    resp = http.post("/ask/stream", json={"question": "q"})  # same caller, same limit
    assert resp.status_code == 429
    assert 1 <= int(resp.headers["Retry-After"]) <= 60
    assert len(fake_client.messages.calls) == 2  # the rejected requests cost nothing


def test_limit_is_per_api_key(http, fake_client, fake_retrieve, monkeypatch):
    monkeypatch.setenv("RAG_RATE_LIMIT_PER_MINUTE", "1")
    monkeypatch.setenv("RAG_API_KEY", "k1")
    assert http.post("/ask", json={"question": "q"}, headers={"X-API-Key": "k1"}).status_code == 200
    assert http.post("/ask", json={"question": "q"}, headers={"X-API-Key": "k1"}).status_code == 429


def test_rejected_key_does_not_use_up_the_limit(http, fake_client, fake_retrieve, monkeypatch):
    monkeypatch.setenv("RAG_RATE_LIMIT_PER_MINUTE", "1")
    monkeypatch.setenv("RAG_API_KEY", "k1")
    assert http.post("/ask", json={"question": "q"}, headers={"X-API-Key": "wrong"}).status_code == 401
    assert http.post("/ask", json={"question": "q"}, headers={"X-API-Key": "k1"}).status_code == 200
