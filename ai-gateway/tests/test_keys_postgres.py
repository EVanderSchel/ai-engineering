"""API keys in PostgreSQL: the admin commands, the key store, and the gateway checking keys against the database.
Needs PostgreSQL (see conftest.database_url)."""

import asyncio
import sys

import psycopg
import pytest
from conftest import routes
from fastapi.testclient import TestClient

import admin
import app as gateway
import db
import keys
import migrate
from stub_provider import StubProvider


@pytest.fixture
def conn(database_url):
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate.apply(connection, migrate.load())
        # The test routes' Claude stand-in needs a price, or the gateway refuses to start.
        connection.execute("insert into prices values ('anthropic', 'claude-test', 1, 5, 0.1, 1.25, '2026-01-01')")
        yield connection


def audit_actions(conn) -> list[str]:
    return [row[0] for row in conn.execute("select action from audit_log order by id")]


# --- admin commands ---


def test_adding_a_team_and_issuing_a_key_is_audited(conn):
    admin.add_team(conn, "research", ["fast"])
    key = admin.issue_key(conn, "research")
    stored = conn.execute("select key_hash, prefix from api_keys").fetchall()
    assert stored == [(keys.key_hash(key), key[:10])]  # the hash and prefix, never the key
    assert key not in str(conn.execute("select * from api_keys").fetchall())
    assert audit_actions(conn) == ["team.created", "key.issued"]
    detail = conn.execute("select detail from audit_log where action = 'key.issued'").fetchone()[0]
    assert detail == {"prefix": key[:10]}


def test_admin_errors_are_plain(conn):
    admin.add_team(conn, "research")
    with pytest.raises(admin.AdminError, match="already exists"):
        admin.add_team(conn, "research")
    with pytest.raises(admin.AdminError, match="lowercase"):
        admin.add_team(conn, "Research Team")
    with pytest.raises(admin.AdminError, match="no team"):
        admin.issue_key(conn, "nobody")
    with pytest.raises(admin.AdminError, match="no active key"):
        admin.revoke_key(conn, "gw_missing")
    assert audit_actions(conn) == ["team.created"]  # failed commands leave no trace


def test_revoking_a_key_and_listing(conn):
    admin.add_team(conn, "research")
    key = admin.issue_key(conn, "research")
    admin.issue_key(conn, "research")
    admin.revoke_key(conn, key[:10])
    states = [revoked is not None for _, _, revoked in admin.list_keys(conn, "research")]
    assert states == [True, False]
    assert admin.list_teams(conn)[0][:3] == ("research", "all", 1)
    with pytest.raises(admin.AdminError):
        admin.revoke_key(conn, key[:10])  # already revoked


def test_the_cli_issues_a_key_it_prints_once(conn, database_url, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", database_url)
    admin.main(["team", "add", "cli-team", "--routes", "smart, fast"])
    admin.main(["key", "issue", "cli-team"])
    printed = capsys.readouterr().out
    key = next(word for word in printed.split() if word.startswith("gw_"))
    assert conn.execute("select count(*) from api_keys where key_hash = %s", (keys.key_hash(key),)).fetchone() == (1,)
    assert conn.execute("select allowed_routes from teams").fetchone() == (["smart", "fast"],)


# --- the key store ---


@pytest.mark.anyio
async def test_the_store_finds_active_keys_only(conn, database_url):
    admin.add_team(conn, "research", ["fast"])
    key = admin.issue_key(conn, "research")
    async with db.make_pool(database_url) as pool:
        store = keys.PostgresKeyStore(pool)
        team = await store.team_for(key)
        assert (team.name, team.allowed_routes) == ("research", ("fast",))
        assert await store.team_for(keys.new_key()) is None
        admin.revoke_key(conn, key[:10])
        assert await store.team_for(key) is None  # revocation takes effect on the next request


# --- the gateway against the database ---


@pytest.fixture
def db_client(conn, database_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", database_url)
    app = gateway.create_app(routes(), {"stub": StubProvider(), "anthropic": StubProvider("anthropic")})
    # psycopg's async connections need the selector event loop on Windows (see conftest.anyio_backend).
    options = {"loop_factory": asyncio.SelectorEventLoop} if sys.platform == "win32" else {}
    with TestClient(app, backend_options=options) as client:  # `with` runs the app's startup: the pool opens
        yield client


def ask(client, key, model="stub"):
    body = {"model": model, "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]}
    return client.post("/v1/chat/completions", json=body, headers={"authorization": f"Bearer {key}"})


def test_the_gateway_checks_keys_in_the_database(conn, db_client):
    admin.add_team(conn, "research")
    key = admin.issue_key(conn, "research")
    assert ask(db_client, key).status_code == 200
    assert ask(db_client, keys.new_key()).status_code == 401
    admin.revoke_key(conn, key[:10])
    response = ask(db_client, key)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"


def test_a_team_limited_to_some_routes_gets_403_elsewhere(conn, db_client):
    admin.add_team(conn, "limited", ["stub"])
    key = admin.issue_key(conn, "limited")
    assert ask(db_client, key, "stub").status_code == 200
    refused = ask(db_client, key, "claude")
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "route_not_allowed"
    listed = db_client.get("/v1/models", headers={"authorization": f"Bearer {key}"}).json()["data"]
    assert [m["id"] for m in listed] == ["stub"]


def test_an_unreachable_database_is_a_503_not_a_401(conn, db_client):
    admin.add_team(conn, "research")
    key = admin.issue_key(conn, "research")

    class Down:
        async def team_for(self, key):
            raise psycopg.OperationalError("connection refused")

    app = gateway.create_app(routes(), {"stub": StubProvider(), "anthropic": StubProvider("anthropic")}, Down())
    response = ask(TestClient(app), key)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "auth_unavailable"
