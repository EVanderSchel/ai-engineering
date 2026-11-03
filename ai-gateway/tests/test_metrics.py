"""Prometheus metrics (metrics.py): each request counted from its log line's fields, labels kept to values the
gateway controls, breakers and budgets read when scraped, and the endpoint on its own port."""

import socket
from decimal import Decimal

import httpx
import psycopg
import pytest
from conftest import AUTH, TEST_KEY, no_wait_policy, routes
from fastapi.testclient import TestClient

import admin
import app as gateway
import config
import db
import keys
import metrics
import migrate
import router
from providers import ProviderError
from stub_provider import StubProvider


def value(app, name, **labels):
    return app.state.metrics.registry.get_sample_value(name, labels) or 0


def chat(client, **overrides):
    body = {"model": "stub", "max_tokens": 50, "messages": [{"role": "user", "content": "hello there"}]}
    return client.post("/v1/chat/completions", json=body | overrides, headers=AUTH)


def test_a_completion_is_counted_with_its_time_attempt_and_tokens(app, client):
    usage = chat(client).json()["usage"]
    assert value(app, "gateway_requests_total", route="stub", status="200", outcome="ok") == 1
    assert value(app, "gateway_request_duration_seconds_count", route="stub", stream="false") == 1
    assert value(app, "gateway_provider_attempts_total", provider="stub", model="stub-echo", outcome="ok") == 1
    assert value(app, "gateway_tokens_total", team="test", route="stub", kind="input") == usage["prompt_tokens"]
    assert value(app, "gateway_tokens_total", team="test", route="stub", kind="output") == usage["completion_tokens"]
    assert value(app, "gateway_requests_in_flight") == 0


def test_a_stream_records_its_first_token_and_finishes_in_flight(app, client):
    with client.stream("POST", "/v1/chat/completions", headers=AUTH, json={
        "model": "stub", "max_tokens": 50, "stream": True, "messages": [{"role": "user", "content": "hi"}],
    }) as response:  # fmt: skip
        assert value(app, "gateway_requests_in_flight") == 1  # headers sent, body not yet
        response.read()
    assert value(app, "gateway_first_token_seconds_count", route="stub", provider="stub") == 1
    assert value(app, "gateway_request_duration_seconds_count", route="stub", stream="true") == 1
    assert value(app, "gateway_requests_in_flight") == 0


def test_refusals_are_counted_and_made_up_route_names_are_not_labels(app, client):
    chat(client, model="anything-a-caller-types")
    client.post("/v1/chat/completions", json={"model": "stub", "max_tokens": 5,
                                              "messages": [{"role": "user", "content": "hi"}]})  # fmt: skip
    client.get("/healthz")
    assert value(app, "gateway_requests_total", route="unknown", status="404", outcome="refused") == 1
    assert value(app, "gateway_requests_total", route="stub", status="401", outcome="refused") == 1
    assert "anything-a-caller-types" not in app.state.metrics.render().decode()
    assert "/healthz" not in app.state.metrics.render().decode()  # probes aren't requests here


def test_a_fallback_and_the_failed_attempts_before_it_are_counted():
    route = config.Route(
        candidates=[
            config.Candidate(provider="down", model="m1"),
            config.Candidate(provider="stub", model="stub-echo"),
        ],
        max_tokens_ceiling=100,
    )
    down = StubProvider("down", fail=ProviderError("unavailable", "down", "no"))
    app = gateway.create_app(
        {"stub": route}, {"down": down, "stub": StubProvider()}, {keys.key_hash(TEST_KEY): "test"},
        policy=no_wait_policy(),
    )  # fmt: skip
    assert chat(TestClient(app)).status_code == 200
    assert value(app, "gateway_provider_attempts_total", provider="down", model="m1", outcome="unavailable") == 2
    assert value(app, "gateway_fallbacks_total", route="stub", provider="stub") == 1


def test_guardrail_findings_are_counted_by_check_and_action_only():
    route = config.Route(
        candidates=[config.Candidate(provider="stub", model="stub-echo")], max_tokens_ceiling=100,
        guardrails=config.Guardrails(pii_input="flag"),
    )  # fmt: skip
    app = gateway.create_app({"stub": route}, {"stub": StubProvider()}, {keys.key_hash(TEST_KEY): "test"})
    chat(TestClient(app), messages=[{"role": "user", "content": "write to ann@example.org"}])
    assert value(app, "gateway_guardrail_findings_total", check="pii_input", action="flagged") == 1


def test_an_open_breaker_shows_until_it_closes():
    breakers = router.Breakers(lambda: router.Breaker(threshold=1))
    gauges = metrics.Metrics(routes(), breakers)
    candidate = config.Candidate(provider="anthropic", model="claude-test")
    breakers[candidate].failure()
    sample = lambda: gauges.registry.get_sample_value(  # noqa: E731
        "gateway_breaker_open", {"provider": "anthropic", "model": "claude-test"}
    )
    assert sample() == 1
    breakers[candidate].success()
    assert sample() == 0


def test_budgets_show_spend_and_only_real_caps():
    gauges = metrics.Metrics(routes(), router.Breakers())
    gauges.budgets = [("research", 1.25, 10.0), ("ops", 0.5, None)]
    get = gauges.registry.get_sample_value
    assert get("gateway_budget_spent_usd", {"team": "research"}) == 1.25
    assert get("gateway_budget_limit_usd", {"team": "research"}) == 10.0
    assert get("gateway_budget_spent_usd", {"team": "ops"}) == 0.5
    assert get("gateway_budget_limit_usd", {"team": "ops"}) is None  # no cap: no series, not a zero cap


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_metrics_are_served_on_their_own_port_and_not_on_the_api(stub, claude_stub):
    port = free_port()
    app = gateway.create_app(
        routes(), {"stub": stub, "anthropic": claude_stub}, {keys.key_hash(TEST_KEY): "test"}, metrics_port=port
    )
    with TestClient(app) as client:
        chat(client)
        scraped = httpx.get(f"http://127.0.0.1:{port}/metrics").text  # this machine only, never the network
        assert 'gateway_requests_total{outcome="ok",route="stub",status="200"} 1.0' in scraped
        assert client.get("/metrics").status_code == 404  # callers can't read other teams' figures
    # Stopped with the app. Refused on Linux; on Windows a closed local port retries for about 2 s, so a timeout.
    with pytest.raises((httpx.ConnectError, httpx.ConnectTimeout)):
        httpx.get(f"http://127.0.0.1:{port}/metrics", timeout=1)


@pytest.mark.anyio
async def test_budgets_are_read_from_the_database(database_url):
    with psycopg.connect(database_url, autocommit=True) as conn:
        migrate.apply(conn, migrate.load())
        admin.add_team(conn, "research")
        admin.add_team(conn, "ops")
        admin.set_budget(conn, "research", Decimal("10.00"))
        conn.execute(
            "insert into budgets (team_id, month, spent_usd) select id, date_trunc('month', now() at time zone 'utc'), "
            "1.25 from teams where name = 'research'"
        )
    pool = db.make_pool(database_url)
    await pool.open(wait=True)
    async with pool:
        gauges = metrics.Metrics(routes(), router.Breakers())
        await gauges.refresh_budgets(pool)
    assert sorted(gauges.budgets) == [("ops", 0.0, None), ("research", 1.25, 10.0)]
