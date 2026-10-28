"""Shared test setup: an app wired to stub providers and one test key. Nothing here reaches a network."""

import asyncio
import os
import sys
import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import conninfo, sql

import app as gateway
import config
import keys
import migrate
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
    # psycopg's async mode can't use Windows' default (Proactor) event loop: it hangs. Linux is unaffected.
    if sys.platform == "win32":
        return "asyncio", {"loop_factory": asyncio.SelectorEventLoop}
    return "asyncio"


@pytest.fixture
def database_url():
    """A fresh, empty PostgreSQL database for one test, dropped afterwards.

    Needs a server: DATABASE_URL (CI's service container) or the local one from docker-compose.yml via .env. Without
    one, database tests are skipped rather than failed, so the rest of the suite still runs anywhere.
    """
    try:
        admin_url = migrate.database_url()
        admin = psycopg.connect(admin_url, autocommit=True, connect_timeout=3)
    except (migrate.MigrationError, psycopg.OperationalError) as error:
        if os.environ.get("REQUIRE_DATABASE"):  # set in CI: there, a missing database is a failure, not a skip
            pytest.fail(f"REQUIRE_DATABASE is set but PostgreSQL isn't reachable: {error}")
        pytest.skip(f"no PostgreSQL for database tests: {error}")
    name = f"gateway_test_{uuid.uuid4().hex[:12]}"
    with admin:
        admin.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
        try:
            yield conninfo.make_conninfo(admin_url, dbname=name)
        finally:
            admin.execute(sql.SQL("drop database {} with (force)").format(sql.Identifier(name)))


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
