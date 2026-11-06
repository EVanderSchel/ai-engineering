"""/readyz and the startup schema check. Most need PostgreSQL (see conftest.database_url)."""

import asyncio
import sys
import time

import psycopg
import psycopg_pool
import pytest
from conftest import AUTH, routes
from fastapi.testclient import TestClient

import app as gateway
import db
import migrate
from stub_provider import StubProvider

SELECTOR = {"loop_factory": asyncio.SelectorEventLoop} if sys.platform == "win32" else {}


def providers():
    return {"stub": StubProvider(), "anthropic": StubProvider("anthropic")}


def migrated(url: str) -> None:
    with psycopg.connect(url, autocommit=True) as conn:
        migrate.apply(conn, migrate.load())
        conn.execute("insert into prices values ('anthropic', 'claude-test', 1, 5, 0.1, 1.25, '2026-01-01')")


def test_without_a_database_the_gateway_is_ready(client):
    assert client.get("/readyz").json() == {"status": "ready", "database": "not used"}
    assert client.get("/healthz", headers=AUTH).status_code == 200


def test_with_a_reachable_database_it_is_ready(database_url, monkeypatch):
    migrated(database_url)
    monkeypatch.setenv("DATABASE_URL", database_url)
    with TestClient(gateway.create_app(routes(), providers()), backend_options=SELECTOR) as client:
        assert client.get("/readyz").json() == {"status": "ready", "database": "ok"}


def test_when_the_database_stops_answering_it_is_not_ready_but_still_alive(database_url, monkeypatch):
    migrated(database_url)
    monkeypatch.setenv("DATABASE_URL", database_url)

    async def no_answer(self):
        return False

    with TestClient(gateway.create_app(routes(), providers()), backend_options=SELECTOR) as client:
        monkeypatch.setattr(db.Health, "check", no_answer)  # the background check now fails every time
        time.sleep(0.5)  # a real check already under way (milliseconds) finishes first
        health = client.app.state.health
        health.last_ok -= health.stale_after + 1  # and its last success is older than it may be
        response = client.get("/readyz")
        assert response.status_code == 503
        assert response.json() == {"status": "not ready", "database": "unreachable"}
        assert client.get("/healthz").status_code == 200  # alive: Kubernetes stops sending traffic, doesn't restart


def test_a_busy_pool_does_not_make_it_unready(database_url, monkeypatch):
    """Stage 5's load test: every pool connection lent out to queued requests made /readyz fail, and Kubernetes took
    busy pods out of service. Readiness no longer borrows from the pool."""
    migrated(database_url)
    monkeypatch.setenv("DATABASE_URL", database_url)

    def pool_exhausted(self, timeout=None):
        raise psycopg_pool.PoolTimeout("every connection is lent out")

    with TestClient(gateway.create_app(routes(), providers()), backend_options=SELECTOR) as client:
        monkeypatch.setattr(psycopg_pool.AsyncConnectionPool, "connection", pool_exhausted)
        assert client.get("/readyz").json() == {"status": "ready", "database": "ok"}
        # requests themselves still fail fast with a clear error, as before
        body = {"model": "stub", "messages": [{"role": "user", "content": "hi"}]}
        assert client.post("/v1/chat/completions", json=body, headers=AUTH).status_code in (401, 503)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.mark.anyio
async def test_health_is_ready_until_its_last_success_goes_stale(database_url):
    clock = FakeClock()
    health = db.Health(database_url, stale_after=6, clock=clock)
    try:
        assert not health.ready()  # nothing checked yet
        assert await health.check()
        assert health.ready()
        clock.now += 6
        assert health.ready()  # exactly at the limit
        clock.now += 0.1
        assert not health.ready()
        assert await health.check()  # the same connection, still open
        assert health.ready()
    finally:
        await health.close()


@pytest.mark.anyio
async def test_health_check_fails_quickly_and_retries_on_an_unreachable_server():
    health = db.Health("postgresql://nobody@127.0.0.1:1/none", timeout=1)
    started = asyncio.get_running_loop().time()
    assert not await health.check()
    assert not health.ready()
    assert asyncio.get_running_loop().time() - started < 3
    assert health._conn is None  # dropped, so the next check reconnects
    await health.close()


@pytest.mark.anyio
async def test_ping_gives_up_quickly_on_an_unreachable_server():
    pool = db.make_pool("postgresql://nobody@127.0.0.1:1/none")
    await pool.open(wait=False)
    try:
        started = asyncio.get_running_loop().time()
        with pytest.raises(db.UNAVAILABLE):
            await db.ping(pool, timeout=0.5)
        assert asyncio.get_running_loop().time() - started < 3
    finally:
        await pool.close()


@pytest.mark.anyio
async def test_the_schema_check_names_what_is_missing_or_unknown(database_url):
    async with db.make_pool(database_url) as pool:
        with pytest.raises(RuntimeError, match="not applied yet.*001_teams_and_keys"):
            await db.check_schema(pool)
        migrated(database_url)
        await db.check_schema(pool)  # up to date: no error
        with psycopg.connect(database_url, autocommit=True) as conn:
            conn.execute(
                "insert into schema_migrations (version, name, checksum) values (99, 'future', %s)", ("0" * 64,)
            )
        with pytest.raises(RuntimeError, match="unknown to this code.*099_future"):
            await db.check_schema(pool)


def test_the_gateway_refuses_to_start_on_an_unmigrated_database(database_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", database_url)
    with pytest.raises(RuntimeError, match="run src/migrate.py"):
        with TestClient(gateway.create_app(routes(), providers()), backend_options=SELECTOR):
            pass
