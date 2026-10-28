"""A provider that answers offline and for free, and can be told to fail: for tests, local development, load tests and
the resilience measurements.

It echoes the last user message, so a test can tell which request a response belongs to, and counts words as
tokens. Faults are injected on purpose:

- `fail` with `fail_times`: raise this error on the first N calls (or every call), before any output, or partway
  through a stream (`fail_after_words`);
- `error_rate`: raise `fail` on a random share of calls (seeded, so a measurement can be repeated exactly);
- `first_token_delay` and `delay`: a slow start, and a slow pace per word.

Plain language: a crash-test dummy. It lets every way a provider can go wrong be staged on demand, without waiting
for a real outage or paying for real calls.
"""

import asyncio
import random
from collections.abc import AsyncIterator

from providers import Completion, Finish, ProviderError, ProviderRequest, StreamEvent, TextDelta, Usage


class StubProvider:
    supports_sampling = True

    def __init__(
        self,
        name: str = "stub",
        fail: ProviderError | None = None,
        fail_times: int | None = None,
        fail_after_words: int = 0,
        error_rate: float = 0.0,
        first_token_delay: float = 0.0,
        delay: float = 0.0,
        seed: int = 0,
    ):
        self.name = name
        self.fail = fail
        self.fail_times = fail_times  # None: every call fails while `fail` is set (unless error_rate is used)
        self.fail_after_words = fail_after_words  # 0: fail before any output
        self.error_rate = error_rate
        self.first_token_delay = first_token_delay  # seconds before the first word (or the whole answer)
        self.delay = delay  # seconds per word
        self.random = random.Random(seed)
        self.calls = 0
        self.requests: list[ProviderRequest] = []  # what it was sent, for tests to inspect

    def _fails_now(self) -> bool:
        if self.fail is None:
            return False
        if self.error_rate:
            return self.random.random() < self.error_rate
        return self.fail_times is None or self.calls <= self.fail_times

    def _error(self) -> ProviderError:
        error = self.fail
        return ProviderError(error.kind, error.message, self.name, error.retry_after)

    def _answer(self, request: ProviderRequest) -> tuple[list[str], str]:
        words = f"Stub reply to: {request.turns[-1].text}".split()
        if len(words) > request.max_tokens:
            return words[: request.max_tokens], "length"
        return words, "stop"

    def _usage(self, request: ProviderRequest, output_words: int) -> Usage:
        prompt_words = sum(len(t.text.split()) for t in request.turns) + len((request.system or "").split())
        return Usage(input_tokens=prompt_words, output_tokens=output_words)

    async def complete(self, request: ProviderRequest) -> Completion:
        self.calls += 1
        self.requests.append(request)
        fails = self._fails_now()
        await asyncio.sleep(self.first_token_delay)
        if fails:
            raise self._error()
        words, reason = self._answer(request)
        await asyncio.sleep(self.delay * len(words))
        return Completion(" ".join(words), reason, request.model, self._usage(request, len(words)))

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        self.requests.append(request)
        fails = self._fails_now()
        await asyncio.sleep(self.first_token_delay)
        words, reason = self._answer(request)
        for i, word in enumerate(words):
            if fails and i == self.fail_after_words:
                raise self._error()
            await asyncio.sleep(self.delay)
            yield TextDelta(word if i == 0 else f" {word}")
        yield Finish(reason, request.model, self._usage(request, len(words)))
