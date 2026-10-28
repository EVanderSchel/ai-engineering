"""Trying a route's candidates in order: retries, fallback and circuit breakers (design decisions 8-11).

Plain language: when the first choice of model is down or slow, the gateway tries again once if the problem looks
temporary, then moves on to the next model on the route's list, skipping any model whose "breaker" has tripped
because it kept failing. A request has one overall deadline; nothing starts that can't finish before it.

Streams can only switch model before their first byte (decision 10), so for a stream "succeeded" means "the first
event arrived"; what happens after that is the stream's own business.
"""

import asyncio
import logging
import random
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

from config import Candidate, Route
from providers import Completion, Provider, ProviderError, ProviderRequest, StreamEvent

log = logging.getLogger("gateway.router")

# Errors worth retrying on the same candidate: the provider is temporarily unable, not the request being wrong.
RETRY_SAME = {"unavailable"}
# Errors that say the request itself is bad: it would fail on every candidate too, so stop (decision 9).
STOP = {"rejected", "too_large"}
# Errors that count against a candidate's circuit breaker. A bad request isn't the provider's fault.
BREAKER_FAILURES = {"timeout", "unavailable", "misconfigured", "bad_response"}


@dataclass
class Policy:
    retries: int = 1  # extra attempts on the same candidate, for RETRY_SAME errors
    backoff: float = 0.25  # seconds: the first retry waits up to this, doubling each time (full jitter)
    max_wait: float = 2.0  # never wait longer than this before a retry, even if the provider asks for more
    random: random.Random = field(default_factory=random.Random)


class Breaker:
    """One per provider model, per gateway replica (decision 11).

    closed: requests pass. After `threshold` failures within `window` seconds it opens: the candidate is skipped
    without waiting on it. After `cooldown` seconds it lets one trial request through (half-open); a success closes
    it, a failure opens it again.
    """

    def __init__(self, threshold: int = 5, window: float = 30.0, cooldown: float = 30.0, clock=time.monotonic):
        self.threshold, self.window, self.cooldown, self.clock = threshold, window, cooldown, clock
        self.failures: deque[float] = deque()
        self.opened_at: float | None = None
        self.trial_in_flight = False

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        return "half_open" if self.clock() - self.opened_at >= self.cooldown else "open"

    def allow(self) -> bool:
        state = self.state
        if state == "closed":
            return True
        if state == "half_open" and not self.trial_in_flight:
            self.trial_in_flight = True
            return True
        return False

    def success(self) -> None:
        self.failures.clear()
        self.opened_at, self.trial_in_flight = None, False

    def release(self) -> None:
        """The attempt ended in a way that says nothing about the provider (a bad request): free the trial slot."""
        self.trial_in_flight = False

    def failure(self) -> None:
        now = self.clock()
        if self.opened_at is not None:  # the half-open trial failed: open again for another cooldown
            self.opened_at, self.trial_in_flight = now, False
            return
        self.failures.append(now)
        while self.failures and now - self.failures[0] > self.window:
            self.failures.popleft()
        if len(self.failures) >= self.threshold:
            self.opened_at = now
            log.warning("circuit breaker opened after %d failures in %.0f s", len(self.failures), self.window)


class Breakers:
    def __init__(self, factory: Callable[[], Breaker] = Breaker):
        self.factory = factory
        self.by_candidate: dict[tuple[str, str], Breaker] = {}

    def __getitem__(self, candidate: Candidate) -> Breaker:
        key = (candidate.provider, candidate.model)
        if key not in self.by_candidate:
            self.by_candidate[key] = self.factory()
        return self.by_candidate[key]


@dataclass
class Attempt:
    provider: str
    model: str
    outcome: str  # "ok", an error kind, or "skipped" (breaker open)
    seconds: float = 0.0


class AllFailed(Exception):
    """No candidate answered. `error` is the one the caller should see."""

    def __init__(self, error: ProviderError | None, attempts: list[Attempt], timed_out: bool):
        super().__init__(error.message if error else "no candidate could be tried")
        self.error, self.attempts, self.timed_out = error, attempts, timed_out


@dataclass
class Result:
    candidate: Candidate
    attempts: list[Attempt]


async def _tries(
    route: Route, providers: dict[str, Provider], breakers: Breakers, policy: Policy, deadline: float, attempt_fn
):
    """The shared loop. `attempt_fn(provider, candidate, timeout)` makes one attempt and returns its value."""
    loop = asyncio.get_running_loop()
    attempts: list[Attempt] = []
    last_error: ProviderError | None = None
    timed_out = False
    for candidate in route.candidates:
        breaker = breakers[candidate]
        if not breaker.allow():
            attempts.append(Attempt(candidate.provider, candidate.model, "skipped"))
            continue
        provider = providers[candidate.provider]
        for retry in range(policy.retries + 1):
            remaining = deadline - loop.time()
            if remaining <= 0:
                timed_out = True
                break
            started = loop.time()
            try:
                value = await attempt_fn(provider, candidate, remaining)
            except ProviderError as error:
                attempts.append(Attempt(candidate.provider, candidate.model, error.kind, loop.time() - started))
                last_error = error
                if error.kind in BREAKER_FAILURES:
                    breaker.failure()
                if error.kind in STOP:
                    breaker.release()
                    raise AllFailed(error, attempts, timed_out=False) from None
                if error.kind not in RETRY_SAME or retry == policy.retries or breaker.state != "closed":
                    break
                wait = min(policy.max_wait, error.retry_after or policy.random.uniform(0, policy.backoff * 2**retry))
                if loop.time() + wait >= deadline:
                    break
                await asyncio.sleep(wait)
                continue
            breaker.success()
            attempts.append(Attempt(candidate.provider, candidate.model, "ok", loop.time() - started))
            return value, Result(candidate, attempts)
        if timed_out:
            break
    raise AllFailed(last_error, attempts, timed_out or loop.time() >= deadline)


async def complete(
    route: Route,
    providers: dict[str, Provider],
    breakers: Breakers,
    build: Callable[[Candidate], ProviderRequest],
    deadline: float,
    policy: Policy | None = None,
) -> tuple[Completion, Result]:
    async def attempt(provider: Provider, candidate: Candidate, remaining: float) -> Completion:
        timeout = min(route.timeout.attempt, remaining)
        try:
            async with asyncio.timeout(timeout):
                return await provider.complete(build(candidate))
        except TimeoutError:
            raise ProviderError("timeout", f"no answer in {timeout:.1f} s", provider.name) from None

    return await _tries(route, providers, breakers, policy or Policy(), deadline, attempt)


async def stream(
    route: Route,
    providers: dict[str, Provider],
    breakers: Breakers,
    build: Callable[[Candidate], ProviderRequest],
    deadline: float,
    policy: Policy | None = None,
) -> tuple[tuple[StreamEvent, AsyncIterator[StreamEvent]], Result]:
    """The first event and the rest of the stream, from the first candidate that starts answering in time."""

    async def attempt(provider: Provider, candidate: Candidate, remaining: float):
        events = provider.stream(build(candidate))
        timeout = min(route.timeout.first_token, remaining)
        try:
            async with asyncio.timeout(timeout):
                first = await anext(events)
        except TimeoutError:
            await events.aclose()
            raise ProviderError("timeout", f"no first token in {timeout:.1f} s", provider.name) from None
        except StopAsyncIteration:
            raise ProviderError("bad_response", "the stream ended before any event", provider.name) from None
        except ProviderError:
            await events.aclose()
            raise
        return first, events

    return await _tries(route, providers, breakers, policy or Policy(), deadline, attempt)
