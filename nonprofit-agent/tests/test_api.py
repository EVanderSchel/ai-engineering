"""The HTTP API, with the agent replaced by a fake: every check in front of Claude, and the response shape.
No API calls, no network."""

import datetime
import logging

import pytest
from fastapi.testclient import TestClient

import agent
import api
import rate_limit
import spend

KEY = "test-key"
QUESTION = {"question": "What was Iowa Public Radio's revenue in 2023?"}


def answered(question, **kwargs):
    result = agent.Result(question=question, model=agent.MODEL, answer="Revenue was $12,797,138.", stop="answered")
    result.steps, result.served_models = 2, [agent.MODEL, agent.MODEL]
    result.usage.input_tokens = 10_000  # $0.02 at Sonnet 5 prices
    result.trajectory = [
        {"type": "tool", "name": "get_financials", "input": {"ein": "1"}, "output": "{}", "is_error": False}
    ]
    return result


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    monkeypatch.setenv("NONPROFIT_API_KEY", KEY)
    monkeypatch.setenv("NONPROFIT_RATE_LIMIT_PER_MINUTE", "5")
    monkeypatch.setenv("NONPROFIT_DAILY_SPEND_CAP_USD", "2.00")
    monkeypatch.setattr(api, "client", object())  # "a key is configured"; the fake agent never uses it
    monkeypatch.setattr(api.agent, "run", lambda client, question: answered(question))
    rate_limit.reset()
    spend.reset()
    yield
    rate_limit.reset()
    spend.reset()


@pytest.fixture
def http():
    return TestClient(api.app)


def ask(http, body=QUESTION, key=KEY):
    return http.post("/ask", json=body, headers={"X-API-Key": key} if key else {})


def test_health_needs_no_key(http):
    response = http.get("/health")
    assert response.status_code == 200
    assert response.json()["auth_required"] is True
    assert response.json()["daily_cap_usd"] == 2.0


@pytest.mark.parametrize("key", [None, "wrong-key"])
def test_ask_requires_the_key(http, key):
    assert ask(http, key=key).status_code == 401


def test_an_answer_with_what_the_agent_looked_up(http):
    body = ask(http).json()
    assert body["answer"] == "Revenue was $12,797,138."
    assert (body["stop"], body["explanation"]) == ("answered", None)
    assert body["tool_calls"] == [{"name": "get_financials", "input": {"ein": "1"}, "is_error": False}]
    assert body["cost_usd"] == 0.02
    assert spend.spent_today() == pytest.approx(0.02)  # counted against the daily cap


@pytest.mark.parametrize("question", ["", "hi", "x" * 501])
def test_question_length_is_checked_before_anything_runs(http, question):
    assert ask(http, {"question": question}).status_code == 422


def test_rate_limit(http):
    assert [ask(http).status_code for _ in range(6)] == [200] * 5 + [429]


def test_daily_cap_stops_questions_before_they_start(http, monkeypatch):
    calls = []
    monkeypatch.setattr(api.agent, "run", lambda client, question: calls.append(question) or answered(question))
    spend.record(1.80)  # $1.80 + a $0.25 worst case would pass $2.00
    response = ask(http)
    assert response.status_code == 429
    assert "spend cap" in response.json()["detail"]
    assert int(response.headers["Retry-After"]) > 0
    assert calls == []  # Claude was never called


def test_busy_while_another_question_runs(http):
    assert api._one_at_a_time.acquire(blocking=False)
    try:
        response = ask(http)
    finally:
        api._one_at_a_time.release()
    assert response.status_code == 429 and response.headers["Retry-After"] == str(api.BUSY_RETRY_SECONDS)


def test_a_claude_api_failure_is_a_502_with_the_reason(http, monkeypatch):
    def failing(client, question):
        return agent.Result(question=question, model=agent.MODEL, stop="api_error", error="credit balance too low")

    monkeypatch.setattr(api.agent, "run", failing)
    response = ask(http)
    assert response.status_code == 502 and response.json()["detail"] == "credit balance too low"


def test_a_non_answer_is_explained(http, monkeypatch):
    monkeypatch.setattr(
        api.agent, "run", lambda client, question: agent.Result(question=question, model=agent.MODEL, stop="max_steps")
    )
    body = ask(http).json()
    assert body["answer"] is None and "limit of" in body["explanation"]


def test_no_anthropic_key_means_503(http, monkeypatch):
    monkeypatch.setattr(api, "client", None)
    assert ask(http).status_code == 503


def test_the_log_never_contains_the_question(http, caplog):
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        ask(http)
    assert "Iowa Public Radio" not in caplog.text
    assert f'"question_chars": {len(QUESTION["question"])}' in caplog.text


def test_spend_cap_rolls_over_at_midnight_utc():
    day1, day2 = datetime.date(2026, 10, 12), datetime.date(2026, 10, 13)
    spend.record(1.90, today=day1)
    assert not spend.can_start(0.25, today=day1)
    assert spend.can_start(0.25, today=day2)


def test_responses_name_their_encoding(http):
    """So Windows PowerShell 5.1 shows em dashes and other non-ASCII text in answers correctly."""
    assert ask(http).headers["content-type"] == "application/json; charset=utf-8"
    assert http.get("/health").headers["content-type"] == "application/json; charset=utf-8"
