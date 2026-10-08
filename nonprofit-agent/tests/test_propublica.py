"""The API client, with a fake network: no test here makes a real request."""

import pytest
import requests

import propublica


class FakeResponse:
    def __init__(self, status_code, data=None):
        self.status_code = status_code
        self._data = data

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


@pytest.fixture
def network(monkeypatch, tmp_path):
    """Replace requests.get with a queue of canned responses; record each call. Caches go to tmp_path."""
    monkeypatch.setattr(propublica, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(propublica.time, "sleep", lambda seconds: None)
    calls, responses = [], []

    def fake_get(url, params, headers, timeout):
        calls.append((url, params))
        return responses.pop(0)

    monkeypatch.setattr(propublica.requests, "get", fake_get)
    return calls, responses


def test_a_response_is_cached_and_reused(network):
    calls, responses = network
    responses.append(FakeResponse(200, {"organization": {"name": "X"}}))
    first = propublica.organization("530196605")
    second = propublica.organization("530196605")
    assert first == second == {"organization": {"name": "X"}}
    assert len(calls) == 1


def test_different_parameters_are_cached_separately(network):
    calls, responses = network
    responses.extend([FakeResponse(200, {"page": 0}), FakeResponse(200, {"page": 1})])
    assert propublica.search("food bank", page=0) == {"page": 0}
    assert propublica.search("food bank", page=1) == {"page": 1}
    assert len(calls) == 2


def test_state_filter_uses_the_api_parameter_name(network):
    calls, responses = network
    responses.append(FakeResponse(200, {}))
    propublica.search("food bank", state="IA")
    assert calls[0] == (propublica.API_BASE + "/search.json", {"q": "food bank", "page": 0, "state[id]": "IA"})


def test_404_raises_not_found_and_is_not_cached(network):
    calls, responses = network
    responses.extend([FakeResponse(404), FakeResponse(200, {"organization": {}})])
    with pytest.raises(propublica.NotFound):
        propublica.organization("000000001")
    assert propublica.organization("000000001") == {"organization": {}}  # asked again, not served from cache
    assert len(calls) == 2


def test_server_errors_are_retried(network):
    calls, responses = network
    responses.extend([FakeResponse(503), FakeResponse(429), FakeResponse(200, {"ok": True})])
    assert propublica.fetch("/search.json", {"q": "x"}) == {"ok": True}
    assert len(calls) == 3


def test_gives_up_after_the_last_attempt(network):
    calls, responses = network
    responses.extend([FakeResponse(503)] * 4)
    with pytest.raises(requests.HTTPError):
        propublica.fetch("/search.json", {"q": "x"})
    assert len(calls) == 4
