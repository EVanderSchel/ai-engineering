import pytest
from fastapi.testclient import TestClient

import api

KEY = "test-key-123"


@pytest.fixture
def key_required(monkeypatch):
    monkeypatch.setenv("RAG_API_KEY", KEY)


@pytest.mark.parametrize("path", ["/ask", "/ask/stream"])
def test_missing_key_is_rejected_before_any_work(http, fake_client, fake_retrieve, key_required, path):
    resp = http.post(path, json={"question": "q"})

    assert resp.status_code == 401
    assert fake_retrieve == [] and fake_client.messages.calls == []  # nothing spent


@pytest.mark.parametrize("path", ["/ask", "/ask/stream"])
def test_wrong_key_is_rejected(http, fake_client, fake_retrieve, key_required, path):
    assert http.post(path, json={"question": "q"}, headers={"X-API-Key": "nope"}).status_code == 401


@pytest.mark.parametrize("path", ["/ask", "/ask/stream"])
def test_correct_key_is_accepted(http, fake_client, fake_retrieve, key_required, path):
    assert http.post(path, json={"question": "q"}, headers={"X-API-Key": KEY}).status_code == 200


def test_health_stays_open_and_reports_auth(http, key_required):
    resp = http.get("/health")
    assert resp.status_code == 200
    assert resp.json()["auth_required"] is True


def test_startup_warms_up_models(monkeypatch):
    calls = []
    monkeypatch.setenv("RAG_WARM_UP", "true")
    monkeypatch.setattr(api, "warm_up", lambda rerank: calls.append(rerank))

    with TestClient(api.app):  # entering the context runs the app's startup
        pass

    assert calls == [False]


def test_failed_warm_up_does_not_stop_startup(monkeypatch):
    def broken(rerank):
        raise RuntimeError("no index")

    monkeypatch.setenv("RAG_WARM_UP", "true")
    monkeypatch.setattr(api, "warm_up", broken)

    with TestClient(api.app) as http:
        assert http.get("/health").status_code == 200
