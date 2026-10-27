"""Shared test setup: an app wired to stub providers and one test key. Nothing here reaches a network."""

import pytest
from fastapi.testclient import TestClient

import app as gateway
import config
import keys
from stub_provider import StubProvider

TEST_KEY = "gw_test-key-for-the-test-team-only"
AUTH = {"authorization": f"Bearer {TEST_KEY}"}


class SamplinglessStub(StubProvider):
    """Stands in for a Claude provider: no temperature or top_p."""

    name = "anthropic"
    supports_sampling = False


def routes() -> dict[str, config.Route]:
    return {
        "stub": config.Route(candidates=[config.Candidate(provider="stub", model="stub-echo")], max_tokens_ceiling=100),
        "claude": config.Route(
            candidates=[config.Candidate(provider="anthropic", model="claude-test", effort="low")],
            max_tokens_ceiling=100,
        ),
    }


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def stub():
    return StubProvider()


@pytest.fixture
def claude_stub():
    return SamplinglessStub()


@pytest.fixture
def app(stub, claude_stub):
    return gateway.create_app(routes(), {"stub": stub, "anthropic": claude_stub}, {keys.key_hash(TEST_KEY): "test"})


@pytest.fixture
def client(app):
    return TestClient(app)
