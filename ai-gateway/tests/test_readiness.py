"""/readyz and the startup schema check. Most need PostgreSQL (see conftest.database_url)."""

import asyncio
import sys

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

    async def no_answer(pool, timeout=2.0):
        raise psycopg_pool.PoolTimeout("no answer")

    with TestClient(gateway.create_app(routes(), providers()), backend_options=SELECTOR) as client:
        monkeypatch.setattr(db, "ping", no_answer)
        response = client.get("/readyz")
        assert response.status_code == 503
        assert response.json() == {"status": "not ready", "database": "unreachable"}
        assert client.get("/healthz").status_code == 200  # alive: Kubernetes stops sending traffic, doesn't restart


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
