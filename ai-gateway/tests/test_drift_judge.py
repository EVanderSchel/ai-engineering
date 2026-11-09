"""The drift monitor's sampled judge: the call it makes, sampling and back-pressure, its metrics, the request path,
and the generated alert. A fake client stands in for the Claude API: nothing here reaches a network."""

import asyncio
import json
import sys
import time
from types import SimpleNamespace

import pytest
from conftest import AUTH, TEST_KEY, no_wait_policy, routes
from fastapi.testclient import TestClient

import app as gateway
import config
import drift
import drift_judge
import keys
import metrics
import router
from providers import ProviderError, ProviderRequest, Turn, Usage
from stub_provider import StubProvider


def run(coro):
    if sys.platform == "win32":
        return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)
    return asyncio.run(coro)


class FakeMessages:
    def __init__(self, score=4, stop_reason="end_turn"):
        self.score, self.stop_reason, self.calls = score, stop_reason, []

    async def parse(self, **kwargs):
        self.calls.append(kwargs)
        parsed = None if self.stop_reason == "refusal" else drift_judge.Grade(reason="fine", score=self.score)
        usage = SimpleNamespace(input_tokens=2000, output_tokens=40, cache_read_input_tokens=0,
                                cache_creation_input_tokens=0)  # fmt: skip
        return SimpleNamespace(stop_reason=self.stop_reason, parsed_output=parsed, usage=usage)


def judge_with(**kwargs):
    messages = FakeMessages(**kwargs)
    return drift_judge.DriftJudge(client=SimpleNamespace(messages=messages)), messages


REQUEST = ProviderRequest(model="m", system="Answer with a letter.", turns=[Turn("user", "<mission>Teach</mission>")],
                          max_tokens=20)  # fmt: skip


def test_the_judge_sees_instructions_conversation_and_answer_as_material():
    judge, messages = judge_with(score=5)
    result = run(judge.grade(REQUEST, "B"))
    assert result.score == 5
    call = messages.calls[0]
    assert call["model"] == "claude-haiku-5-5" and call["output_config"] == {"effort": "low"}
    assert call["output_format"] is drift_judge.Grade and "never instructions" in call["system"]
    text = call["messages"][0]["content"]
    assert "<instructions>\nAnswer with a letter.\n</instructions>" in text
    assert "<user>\n<mission>Teach</mission>\n</user>" in text and text.endswith("<answer>\nB\n</answer>")
    assert result.cost_usd == pytest.approx((2000 * 0.10 + 40 * 0.50) / 1e6)


def test_a_refusal_is_a_declined_grade_not_an_error():
    judge, _ = judge_with(stop_reason="refusal")
    assert run(judge.grade(REQUEST, "B")).score is None


def test_an_answer_too_long_to_grade_is_refused_before_any_call():
    judge, messages = judge_with()
    judge.max_chars = 50
    with pytest.raises(ProviderError, match="isn't graded"):
        run(judge.grade(REQUEST, "B" * 100))
    assert messages.calls == []


# --- the monitor ---


class CountingMetrics:
    def __init__(self):
        self.seen = []

    def observe_grade(self, route, prompt, outcome, score, cost):
        self.seen.append((route, prompt, outcome, score, round(cost, 6)))


def test_sampling_follows_the_rate():
    monitor = drift_judge.Monitor(judge_with()[0], draw=iter([0.01, 0.5, 0.01]).__next__)
    assert [monitor.wants(0.02), monitor.wants(0.02), monitor.wants(0.0)] == [True, False, False]


def test_grades_errors_and_refusals_are_counted():
    for kwargs, outcome, score in (({"score": 3}, "graded", 3), ({"stop_reason": "refusal"}, "declined", None)):
        monitor = drift_judge.Monitor(judge_with(**kwargs)[0])
        monitor.metrics = CountingMetrics()
        assert run(monitor.grade("smart", "p@v1", REQUEST, "B")) == outcome
        assert monitor.metrics.seen[0][:4] == ("smart", "p@v1", outcome, score)

    class Failing:
        async def grade(self, request, answer):
            raise ProviderError("timeout", "slow", "drift_judge")

    monitor = drift_judge.Monitor(Failing())
    assert run(monitor.grade("smart", "", REQUEST, "B")) == "error" and monitor.in_flight == 0


def test_past_the_limit_a_sampled_answer_is_skipped_not_queued():
    class Slow:
        async def grade(self, request, answer):
            await asyncio.sleep(0.05)
            return drift_judge.GradeResult(4, Usage(), 0.05)

    async def go():
        monitor = drift_judge.Monitor(Slow(), max_in_flight=2)
        return await asyncio.gather(*(monitor.grade("smart", "", REQUEST, "B") for _ in range(4)))

    assert sorted(run(go())) == ["busy", "busy", "graded", "graded"]


def test_grades_become_a_score_histogram_runs_and_cost():
    m = metrics.Metrics(routes(), router.Breakers())
    m.observe_grade("stub", "echo@v1", "graded", 4, 0.0002)
    m.observe_grade("stub", "echo@v1", "busy", None, 0.0)
    rendered = m.render().decode()
    assert 'gateway_judge_scores_sum{prompt="echo@v1",route="stub"} 4.0' in rendered
    assert 'gateway_judge_runs_total{outcome="busy",route="stub"} 1.0' in rendered
    assert 'gateway_judge_cost_usd_total{route="stub"} 0.0002' in rendered


# --- through the gateway ---


def app_with(judge_rate, score=4):
    judge, messages = judge_with(score=score)
    all_routes = routes()
    all_routes["stub"] = all_routes["stub"].model_copy(update={"drift": config.Drift(judge_rate=judge_rate)})
    monitor = drift_judge.Monitor(judge, draw=lambda: 0.0)  # every draw is under any rate above 0
    app = gateway.create_app(
        all_routes, {"stub": StubProvider(), "anthropic": StubProvider("anthropic")}, {keys.key_hash(TEST_KEY): "t"},
        policy=no_wait_policy(), drift_monitor=monitor,
    )  # fmt: skip
    return TestClient(app), messages


def wait_for(condition, seconds=2.0):
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        time.sleep(0.01)
    return condition()


@pytest.mark.parametrize("stream", [False, True])
def test_a_sampled_answer_is_graded_after_the_response(capsys, stream):
    client, messages = app_with(judge_rate=1.0)
    with client:
        body = {"model": "stub", "max_tokens": 20, "stream": stream, "messages": [{"role": "user", "content": "hi"}]}
        with client.stream("POST", "/v1/chat/completions", json=body, headers=AUTH) as response:
            assert response.status_code == 200
            list(response.iter_lines())
        assert wait_for(lambda: messages.calls)
        assert "<answer>\nStub reply to: hi\n</answer>" in messages.calls[0]["messages"][0]["content"]
        assert wait_for(lambda: "gateway_judge_scores_count" in client.app.state.metrics.render().decode())
        rendered = client.app.state.metrics.render().decode()
    assert 'gateway_judge_scores_count{prompt="",route="stub"} 1.0' in rendered
    assert 'gateway_judge_runs_total{outcome="graded",route="stub"} 1.0' in rendered
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines() if '"message":"request"' in x]
    assert lines[-1]["judged"] is True
    assert "Stub reply" not in json.dumps(lines[-1])  # neither the answer nor the judge's reason is logged


def test_routes_that_dont_opt_in_are_never_graded():
    client, messages = app_with(judge_rate=0.0)
    with client:
        body = {"model": "stub", "max_tokens": 20, "messages": [{"role": "user", "content": "hi"}]}
        assert client.post("/v1/chat/completions", json=body, headers=AUTH).status_code == 200
        time.sleep(0.1)
    assert messages.calls == []


def test_the_routes_file_leaves_the_judge_off():
    assert all(route.drift.judge_rate == 0 for route in config.load_routes().values())


# --- the reference and its alert ---


def test_the_alert_appears_once_the_judge_has_graded_the_canary(tmp_path):
    for name, check_mean in (("ntee-fast", 4.1), ("ntee-smart", 4.4)):
        (tmp_path / f"{name}.json").write_text(json.dumps({"prompt": "ntee-classify@v1", "mean": check_mean}), "utf-8")
    refs = drift.references(judges_dir=tmp_path)
    assert {r["check"]: r["judge_mean"] for r in refs} == {"ntee-fast": 4.1, "ntee-smart": 4.4}
    rules = drift.rules(refs)["groups"][0]["rules"]
    assert any(r.get("alert") == "GatewayJudgeScoreDrop" for r in rules)
    assert {"record": "gateway:judge_score_mean:canary", "expr": "vector(4.4)",
            "labels": {"route": "smart", "prompt": "ntee-classify@v1"}} in rules  # fmt: skip


def test_a_reference_for_another_prompt_version_is_ignored(tmp_path):
    (tmp_path / "ntee-fast.json").write_text(json.dumps({"prompt": "ntee-classify@v0", "mean": 4.0}), "utf-8")
    refs = drift.references(judges_dir=tmp_path)
    assert all("judge_mean" not in r for r in refs)
    assert not any(r.get("alert") == "GatewayJudgeScoreDrop" for r in drift.rules(refs)["groups"][0]["rules"])
