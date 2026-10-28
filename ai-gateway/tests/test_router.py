"""Retries, fallback and circuit breakers (design decisions 8-11), staged with fault-injecting stubs."""

import asyncio

import pytest
from conftest import AUTH, TEST_KEY, no_wait_policy
from fastapi.testclient import TestClient

import app as gateway
import config
import keys
import router
from providers import ProviderError, ProviderRequest, Turn
from stub_provider import StubProvider

DOWN = ProviderError("unavailable", "down", "?")


def two_candidates(**timeouts) -> config.Route:
    return config.Route(
        candidates=[config.Candidate(provider="a", model="model-a"), config.Candidate(provider="b", model="model-b")],
        max_tokens_ceiling=100,
        timeout=config.Timeouts(**timeouts),
    )


def build(candidate: config.Candidate) -> ProviderRequest:
    return ProviderRequest(model=candidate.model, system=None, turns=[Turn("user", "hi")], max_tokens=20)


async def run(route, providers, breakers=None, policy=None, total=5.0):
    deadline = asyncio.get_running_loop().time() + total
    return await router.complete(
        route, providers, breakers or router.Breakers(), build, deadline, policy or no_wait_policy()
    )


# --- fallback and retries ---


@pytest.mark.anyio
async def test_the_first_candidate_answers_when_healthy():
    a, b = StubProvider("a"), StubProvider("b")
    completion, result = await run(two_candidates(), {"a": a, "b": b})
    assert completion.model == "model-a"
    assert [x.outcome for x in result.attempts] == ["ok"]
    assert b.calls == 0


@pytest.mark.anyio
async def test_a_temporary_error_is_retried_once_on_the_same_candidate():
    a, b = StubProvider("a", fail=DOWN, fail_times=1), StubProvider("b")
    completion, result = await run(two_candidates(), {"a": a, "b": b})
    assert completion.model == "model-a"
    assert [x.outcome for x in result.attempts] == ["unavailable", "ok"]
    assert (a.calls, b.calls) == (2, 0)


@pytest.mark.anyio
async def test_a_lasting_outage_falls_back_to_the_next_candidate():
    a, b = StubProvider("a", fail=DOWN), StubProvider("b")
    completion, result = await run(two_candidates(), {"a": a, "b": b})
    assert completion.model == "model-b"  # the answer names the model that gave it (decision 8)
    assert [(x.provider, x.outcome) for x in result.attempts] == [
        ("a", "unavailable"),
        ("a", "unavailable"),
        ("b", "ok"),
    ]


@pytest.mark.anyio
async def test_a_timeout_moves_on_without_retrying_the_slow_candidate():
    a, b = StubProvider("a", first_token_delay=1.0), StubProvider("b")
    completion, result = await run(two_candidates(attempt=0.05), {"a": a, "b": b})
    assert completion.model == "model-b"
    assert [x.outcome for x in result.attempts] == ["timeout", "ok"]
    assert a.calls == 1


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["rejected", "too_large"])
async def test_a_bad_request_stops_without_trying_other_candidates(kind):
    """Decision 9: it would fail everywhere, and every attempt costs money."""
    a, b = StubProvider("a", fail=ProviderError(kind, "no", "a")), StubProvider("b")
    with pytest.raises(router.AllFailed) as failed:
        await run(two_candidates(), {"a": a, "b": b})
    assert failed.value.error.kind == kind
    assert (a.calls, b.calls) == (1, 0)


@pytest.mark.anyio
async def test_a_misconfigured_candidate_is_skipped_to_the_next_without_a_retry():
    a, b = StubProvider("a", fail=ProviderError("misconfigured", "bad key", "a")), StubProvider("b")
    completion, _ = await run(two_candidates(), {"a": a, "b": b})
    assert completion.model == "model-b"
    assert a.calls == 1


@pytest.mark.anyio
async def test_when_everything_fails_every_attempt_is_reported():
    a, b = StubProvider("a", fail=DOWN), StubProvider("b", fail=DOWN)
    with pytest.raises(router.AllFailed) as failed:
        await run(two_candidates(), {"a": a, "b": b})
    assert [x.outcome for x in failed.value.attempts] == ["unavailable"] * 4
    assert not failed.value.timed_out


@pytest.mark.anyio
async def test_nothing_starts_after_the_deadline():
    a, b = StubProvider("a", first_token_delay=1.0), StubProvider("b", first_token_delay=1.0)
    started = asyncio.get_running_loop().time()
    with pytest.raises(router.AllFailed) as failed:
        await run(two_candidates(attempt=0.08), {"a": a, "b": b}, total=0.1)
    assert failed.value.timed_out
    assert asyncio.get_running_loop().time() - started < 0.5


@pytest.mark.anyio
async def test_backoff_honours_retry_after_up_to_the_cap():
    a = StubProvider("a", fail=ProviderError("unavailable", "slow down", "a", retry_after=60), fail_times=1)
    policy = router.Policy(max_wait=0.05)
    started = asyncio.get_running_loop().time()
    completion, _ = await run(two_candidates(), {"a": a, "b": StubProvider("b")}, policy=policy)
    waited = asyncio.get_running_loop().time() - started
    assert completion.model == "model-a"
    assert 0.04 <= waited < 1  # waited the capped 0.05 s, not the 60 the provider asked for


# --- streams: fallback only before the first byte (decision 10) ---


async def first_and_rest(route, providers, policy=None, total=5.0):
    deadline = asyncio.get_running_loop().time() + total
    (first, events), result = await router.stream(
        route, providers, router.Breakers(), build, deadline, policy or no_wait_policy()
    )
    rest = [event async for event in events]
    return [first, *rest], result


@pytest.mark.anyio
async def test_a_stream_falls_back_if_the_first_token_is_too_slow():
    a, b = StubProvider("a", first_token_delay=1.0), StubProvider("b")
    events, result = await first_and_rest(two_candidates(first_token=0.05), {"a": a, "b": b})
    assert result.candidate.provider == "b"
    assert events[-1].model == "model-b"


@pytest.mark.anyio
async def test_a_stream_falls_back_if_the_provider_fails_before_any_output():
    a, b = StubProvider("a", fail=DOWN), StubProvider("b")
    _, result = await first_and_rest(two_candidates(), {"a": a, "b": b})
    assert result.candidate.provider == "b"


@pytest.mark.anyio
async def test_a_stream_that_fails_after_starting_does_not_switch_provider():
    a, b = StubProvider("a", fail=DOWN, fail_after_words=2), StubProvider("b")
    with pytest.raises(ProviderError):
        await first_and_rest(two_candidates(), {"a": a, "b": b})
    assert b.calls == 0


# --- circuit breaker (decision 11) ---


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_a_breaker_opens_after_five_failures_in_the_window():
    clock = Clock()
    breaker = router.Breaker(clock=clock)
    for _ in range(4):
        breaker.failure()
    assert breaker.state == "closed"
    breaker.failure()
    assert breaker.state == "open" and not breaker.allow()


def test_failures_outside_the_window_are_forgotten():
    clock = Clock()
    breaker = router.Breaker(clock=clock)
    for _ in range(4):
        breaker.failure()
    clock.now = 31
    breaker.failure()
    assert breaker.state == "closed"


def test_after_the_cooldown_one_trial_goes_through_and_its_result_decides():
    clock = Clock()
    breaker = router.Breaker(clock=clock)
    for _ in range(5):
        breaker.failure()
    clock.now = 30
    assert breaker.state == "half_open"
    assert breaker.allow()
    assert not breaker.allow()  # only one trial at a time
    breaker.failure()
    assert breaker.state == "open"  # the trial failed: another full cooldown
    clock.now = 60
    assert breaker.allow()
    breaker.success()
    assert breaker.state == "closed" and breaker.allow()


def test_a_bad_request_during_the_trial_frees_the_slot_without_deciding():
    clock = Clock()
    breaker = router.Breaker(clock=clock)
    for _ in range(5):
        breaker.failure()
    clock.now = 30
    assert breaker.allow()
    breaker.release()
    assert breaker.state == "half_open" and breaker.allow()


@pytest.mark.anyio
async def test_an_open_breaker_skips_the_candidate_without_calling_it():
    a, b = StubProvider("a", fail=DOWN), StubProvider("b")
    breakers = router.Breakers()
    for _ in range(3):  # 2 failures per request (one retry): the breaker opens during the third
        await run(two_candidates(), {"a": a, "b": b}, breakers=breakers)
    calls = a.calls
    _, result = await run(two_candidates(), {"a": a, "b": b}, breakers=breakers)
    assert a.calls == calls
    assert [x.outcome for x in result.attempts] == ["skipped", "ok"]


@pytest.mark.anyio
async def test_breakers_are_per_provider_model():
    breakers = router.Breakers()
    one, other = config.Candidate(provider="a", model="m1"), config.Candidate(provider="a", model="m2")
    for _ in range(5):
        breakers[one].failure()
    assert breakers[one].state == "open"
    assert breakers[other].state == "closed"


# --- through the HTTP API ---


def fallback_client(a, b, breakers=None):
    routes = {"r": two_candidates()}
    app = gateway.create_app(
        routes, {"a": a, "b": b}, {keys.key_hash(TEST_KEY): "t"}, breakers=breakers, policy=no_wait_policy()
    )
    return TestClient(app)


def post(client, **fields):
    body = {"model": "r", "max_tokens": 20, "messages": [{"role": "user", "content": "hi"}], **fields}
    return client.post("/v1/chat/completions", json=body, headers=AUTH)


def test_the_response_says_a_fallback_answered():
    response = post(fallback_client(StubProvider("a", fail=DOWN), StubProvider("b")))
    assert response.status_code == 200
    assert response.json()["model"] == "model-b"
    assert response.headers["x-gateway-provider"] == "b"
    assert response.headers["x-gateway-fallback"] == "true"
    assert response.headers["x-gateway-attempts"] == "3"


def test_a_healthy_first_candidate_is_not_a_fallback():
    response = post(fallback_client(StubProvider("a"), StubProvider("b")))
    assert response.headers["x-gateway-fallback"] == "false"
    assert response.headers["x-gateway-attempts"] == "1"


def test_with_every_breaker_open_the_answer_is_an_immediate_503():
    breakers = router.Breakers()
    for candidate in two_candidates().candidates:
        for _ in range(5):
            breakers[candidate].failure()
    a, b = StubProvider("a"), StubProvider("b")
    response = post(fallback_client(a, b, breakers))
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "all_candidates_paused"
    assert response.headers["retry-after"] == "30"
    assert (a.calls, b.calls) == (0, 0)


def test_a_streamed_fallback_through_the_api():
    client = fallback_client(StubProvider("a", fail=DOWN), StubProvider("b"))
    with client.stream("POST", "/v1/chat/completions", headers=AUTH, json={
        "model": "r", "max_tokens": 20, "stream": True, "messages": [{"role": "user", "content": "hi"}],
    }) as response:  # fmt: skip
        assert response.status_code == 200
        assert response.headers["x-gateway-provider"] == "b"
        body = response.read().decode()
    assert '"model":"model-b"' in body and body.rstrip().endswith("data: [DONE]")


def test_the_app_keeps_its_breakers_for_inspection():
    client = fallback_client(StubProvider("a"), StubProvider("b"))
    assert isinstance(client.app.state.breakers, router.Breakers)
