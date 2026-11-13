"""Step 10's comparison setup: the fake upstream both gateways call, the two config files that must describe the same
cases, and the gateway running the comparison routes end to end against the fake upstream, all in memory. No
network, no Docker."""

import asyncio
import json
import sys

import httpx2
import pytest
import yaml
from conftest import AUTH, TEST_KEY, no_wait_policy
from fastapi.testclient import TestClient

import app as gateway
import compare_suite
import config
import fake_upstream
import keys
from openai_compatible_provider import OpenAICompatibleProvider
from paths import ROOT

COMPARE = ROOT / "compare"


@pytest.fixture
def upstream():
    return TestClient(fake_upstream.app)


def ask(client, model, stream=False, max_tokens=50):
    body = {"model": model, "max_tokens": max_tokens, "stream": stream,
            "messages": [{"role": "user", "content": "hello there"}]}  # fmt: skip
    if stream:
        body["stream_options"] = {"include_usage": True}
    return client.post("/v1/chat/completions", json=body)


# --- the fake upstream ---


def test_it_answers_like_an_openai_server(upstream):
    data = ask(upstream, "fake-ok").json()
    assert data["object"] == "chat.completion" and data["model"] == "fake-ok"
    assert data["choices"][0]["message"]["content"] == " ".join(f"word{i}" for i in range(1, 21))
    assert data["choices"][0]["finish_reason"] == "stop"
    assert data["usage"] == {"prompt_tokens": 2, "completion_tokens": 20, "total_tokens": 22}


def test_max_tokens_cuts_the_answer_short(upstream):
    data = ask(upstream, "fake-ok", max_tokens=3).json()
    assert data["choices"][0]["message"]["content"] == "word1 word2 word3"
    assert data["choices"][0]["finish_reason"] == "length"


def test_it_streams_words_then_usage_then_done(upstream):
    lines = [line for line in ask(upstream, "fake-ok", stream=True).text.splitlines() if line.startswith("data: ")]
    assert lines[-1] == "data: [DONE]"
    chunks = [json.loads(line[6:]) for line in lines[:-1]]
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c["choices"])
    assert text == " ".join(f"word{i}" for i in range(1, 21))
    assert chunks[-1]["usage"]["completion_tokens"] == 20 and chunks[-1]["choices"] == []


@pytest.mark.parametrize(("model", "status"), [("fake-503", 503), ("fake-429", 429), ("fake-400", 400)])
def test_the_model_name_stages_a_failure(upstream, model, status):
    response = ask(upstream, model)
    assert response.status_code == status and response.json()["error"]["type"] == "fake_error"
    if status == 429:
        assert response.headers["retry-after"] == "1"


def test_it_lists_its_models_and_reports_health(upstream):
    assert upstream.get("/health").json() == {"status": "ok"}
    assert {m["id"] for m in upstream.get("/v1/models").json()["data"]} >= {"fake-ok", "fake-503", "fake-slow"}


# --- the two configs describe the same cases ---


def test_both_gateways_offer_the_same_cases_on_the_same_upstream_models():
    ours = config.load_routes(COMPARE / "routes.yaml")
    theirs = yaml.safe_load((COMPARE / "litellm.yaml").read_text("utf-8"))
    models = {m["model_name"]: m["litellm_params"] for m in theirs["model_list"]}
    assert set(ours) == set(models) == {"chat", "fallback", "refused", "slow"}
    for name, route in ours.items():
        assert all(c.provider == "local" for c in route.candidates)  # the fake upstream
        assert models[name]["model"] == f"openai/{route.candidates[0].model}"
        assert models[name]["api_base"] == "http://fake-upstream:8000/v1"
    fallbacks = {k: v for f in theirs["router_settings"]["fallbacks"] for k, v in f.items()}
    assert fallbacks == {"fallback": ["chat"], "refused": ["chat"]}  # the second candidates of ours
    assert all(ours[k].candidates[1].model == "fake-ok" for k in fallbacks)
    assert theirs["litellm_settings"]["num_retries"] == 1  # as the gateway retries once
    assert theirs["general_settings"]["master_key"] == "os.environ/LITELLM_MASTER_KEY"  # never the key itself


def test_the_routes_file_can_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("GATEWAY_ROUTES_FILE", str(COMPARE / "routes.yaml"))
    assert set(config.load_routes()) == {"chat", "fallback", "refused", "slow"}
    monkeypatch.delenv("GATEWAY_ROUTES_FILE")
    assert "smart" in config.load_routes()


def test_compose_runs_both_gateways_alike():
    services = yaml.safe_load((ROOT / "docker-compose.yml").read_text("utf-8"))["services"]
    gw, lite = services["gateway"], services["litellm"]
    assert gw["cpus"] == lite["cpus"] == 1.0
    assert "--num_workers" in lite["command"] and lite["command"][lite["command"].index("--num_workers") + 1] == "1"
    assert "@sha256:" in lite["image"]  # pinned by digest
    assert gw["environment"]["GATEWAY_LOCAL_URL"] == "http://fake-upstream:8000/v1"
    assert all(s["profiles"] == ["compare"] for s in (gw, lite, services["fake-upstream"]))


# --- the gateway against the fake upstream, in memory ---


@pytest.fixture
def compared():
    client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=fake_upstream.app), base_url="http://fake/v1")
    app = gateway.create_app(
        config.load_routes(COMPARE / "routes.yaml"), {"local": OpenAICompatibleProvider(client)},
        {keys.key_hash(TEST_KEY): "compare"}, policy=no_wait_policy(),
    )  # fmt: skip
    return TestClient(app)


def call(client, route):
    body = {"model": route, "max_tokens": 50, "messages": [{"role": "user", "content": "hello there"}]}
    return client.post("/v1/chat/completions", json=body, headers=AUTH)


def test_the_gateway_answers_from_the_fake_upstream(compared):
    response = call(compared, "chat")
    assert response.status_code == 200 and response.json()["choices"][0]["message"]["content"].startswith("word1")


def test_an_overloaded_candidate_is_retried_then_skipped(compared):
    response = call(compared, "fallback")
    assert response.status_code == 200 and response.headers["x-gateway-fallback"] == "true"
    assert response.headers["x-gateway-attempts"] == "3"  # 503, its retry, then the second candidate


def test_a_refused_request_is_passed_on_not_retried_elsewhere(compared):
    response = call(compared, "refused")
    assert response.status_code == 400


# --- the parity suite's own logic ---


def run(coro):
    if sys.platform == "win32":
        return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)
    return asyncio.run(coro)


def in_memory_target():
    upstream = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=fake_upstream.app), base_url="http://fake/v1")
    app = gateway.create_app(
        config.load_routes(COMPARE / "routes.yaml"), {"local": OpenAICompatibleProvider(upstream)},
        {keys.key_hash(TEST_KEY): "compare"}, policy=no_wait_policy(),
    )  # fmt: skip
    client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app))
    return client, compare_suite.Target("gateway", "http://gw", admin=None, key=TEST_KEY)


@pytest.mark.parametrize("stream", [False, True])
def test_a_request_is_read_the_same_way_plain_or_streamed(stream):
    client, target = in_memory_target()
    result = run(compare_suite.chat(client, target, stream=stream))
    assert compare_suite.answered_in_full(result) and result["usage"] is True and result["model"] == "fake-ok"
    if not stream:
        assert "x-gateway-provider" in result["headers"]


def test_an_error_is_read_as_its_status_and_code():
    client, target = in_memory_target()
    refused = run(compare_suite.chat(client, target, model="refused"))
    assert refused["status"] == 400 and refused["error"] and "text" not in refused
    assert run(compare_suite.chat(client, target, key="wrong"))["status"] == 401
    assert run(compare_suite.chat(client, target, key=""))["status"] == 401  # no Authorization header at all


def test_upstream_calls_are_counted_by_difference():
    assert compare_suite.delta({"fake-ok": 3}, {"fake-ok": 4, "fake-503": 2}) == {"fake-ok": 1, "fake-503": 2}
    assert compare_suite.delta({"fake-ok": 3}, {"fake-ok": 3}) == {}


def test_the_report_has_a_row_per_case_with_both_sides():
    result = {"date": "2026-11-09T00:00:00+00:00", "litellm_image": "img", "cases": [
        {"name": "plain", "what": "A plain answer", "gateway": {"status": 200}, "litellm": {"status": 200}},
        {"name": "refused", "what": "Refused", "gateway": {"status": 400}, "litellm": {"status": 200}},
    ]}  # fmt: skip
    lines = compare_suite.report(result).splitlines()
    assert (
        "| A plain answer | status: 200 | status: 200 |" in lines and "| Refused | status: 400 | status: 200 |" in lines
    )


def test_the_committed_results_cover_every_case():
    result = json.loads((ROOT / "results" / "compare" / "parity.json").read_text("utf-8"))
    assert [c["name"] for c in result["cases"]] == [name for name, _, _ in compare_suite.CASES]
    assert all(set(c) >= {"gateway", "litellm"} for c in result["cases"])
