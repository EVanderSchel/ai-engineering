"""A provider that answers instantly, offline and for free: for tests, local development and load tests.

It echoes the last user message, so a test can tell which request a response belongs to, and counts words as
tokens. It can be told to fail, before the first word or partway through, to exercise the gateway's error handling;
step 3 extends it into a fault injector.
"""

import asyncio
from collections.abc import AsyncIterator

from providers import Completion, Finish, ProviderError, ProviderRequest, StreamEvent, TextDelta, Usage

NAME = "stub"


class StubProvider:
    name = NAME
    supports_sampling = True

    def __init__(self, fail: ProviderError | None = None, fail_after_words: int = 0, delay: float = 0.0):
        self.fail = fail
        self.fail_after_words = fail_after_words  # 0: fail before any output
        self.delay = delay  # seconds per word
        self.requests: list[ProviderRequest] = []  # what it was sent, for tests to inspect

    def _answer(self, request: ProviderRequest) -> tuple[list[str], str]:
        words = f"Stub reply to: {request.turns[-1].text}".split()
        if len(words) > request.max_tokens:
            return words[: request.max_tokens], "length"
        return words, "stop"

    def _usage(self, request: ProviderRequest, output_words: int) -> Usage:
        prompt_words = sum(len(t.text.split()) for t in request.turns) + len((request.system or "").split())
        return Usage(input_tokens=prompt_words, output_tokens=output_words)

    async def complete(self, request: ProviderRequest) -> Completion:
        self.requests.append(request)
        if self.fail:
            raise self.fail
        words, reason = self._answer(request)
        await asyncio.sleep(self.delay * len(words))
        return Completion(" ".join(words), reason, request.model, self._usage(request, len(words)))

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamEvent]:
        self.requests.append(request)
        words, reason = self._answer(request)
        for i, word in enumerate(words):
            if self.fail and i == self.fail_after_words:
                raise self.fail
            await asyncio.sleep(self.delay)
            yield TextDelta(word if i == 0 else f" {word}")
        yield Finish(reason, request.model, self._usage(request, len(words)))
