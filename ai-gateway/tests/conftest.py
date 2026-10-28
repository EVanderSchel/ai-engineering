"""Shared test setup: an app wired to stub providers and one test key. Nothing here reaches a network."""

import pytest
from fastapi.testclient import TestClient

import app as gateway
import config
import keys
import router
from stub_provider import StubProvider

TEST_KEY = "gw_test-key-for-the-test-team-only"
AUTH = {"authorization": f"Bearer {TEST_KEY}"}


class SamplinglessStub(StubProvider):
    """Stands in for a Claude provider: no temperature or top_p."""

    supports_sampling = False


def routes() -> dict[str, config.Route]:
    return {
        "stub": config.Route(candidates=[config.Candidate(provider="stub", model="stub-echo")], max_tokens_ceiling=100),
        "claude": config.Route(
            candidates=[config.Candidate(provider="anthropic", model="claude-test", effort="low")],
            max_tokens_ceiling=100,
        ),
    }


def no_wait_policy() -> router.Policy:
    """Retries without waiting, so tests of retry behaviour run instantly."""
    return router.Policy(backoff=0.0, max_wait=0.0)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def stub():
    return StubProvider()


@pytest.fixture
def claude_stub():
    return SamplinglessStub(name="anthropic")


@pytest.fixture
def app(stub, claude_stub):
    return gateway.create_app(
        routes(), {"stub": stub, "anthropic": claude_stub}, {keys.key_hash(TEST_KEY): "test"}, policy=no_wait_policy()
    )


@pytest.fixture
def client(app):
    return TestClient(app)
