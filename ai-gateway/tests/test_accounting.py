"""Costs, rate limits, the budget hold and the usage ledger (decision 12). The first tests need no database; the rest
use PostgreSQL (see conftest.database_url), and the last one sends 50 requests at once to show a budget can't be
overspent by concurrency."""

import asyncio
from decimal import Decimal

import httpx2
import psycopg
import pytest
from conftest import routes

import accounting
import admin
import app as gateway
import config
import db
import keys
import migrate
from providers import Usage
from schemas import ChatRequest
from stub_provider import StubProvider

PRICE = accounting.Price(Decimal(2), Decimal(10), Decimal("0.2"), Decimal("2.5"))


def chat(text="hello there", max_tokens=100, stream=False) -> ChatRequest:
    return ChatRequest(model="stub", max_tokens=max_tokens, stream=stream, messages=[{"role": "user", "content": text}])


# --- arithmetic (no database) ---


def test_cost_adds_each_kind_of_token_at_its_rate():
    usage = Usage(input_tokens=1000, output_tokens=500, cache_read_tokens=10_000, cache_write_tokens=2000)
    assert PRICE.cost(usage) == Decimal("0.014")  # 0.002 + 0.005 + 0.002 + 0.005


def test_the_most_a_request_can_cost_assumes_every_input_token_is_a_cache_write():
    assert PRICE.most(1000, 100) == Decimal("0.0035")  # 1000 x 2.5 + 100 x 10, per million


def test_the_input_estimate_is_generous():
    request = chat("x" * 300)
    assert accounting.estimate_input_tokens(request) == 100 + 8 + 32


def test_prices_are_found_by_reported_model_then_configured_model():
    prices = accounting.Prices({("anthropic", "claude-haiku-4-5"): PRICE})
    assert prices.find("anthropic", "claude-haiku-4-5-20251001", "claude-haiku-4-5") is PRICE
    assert prices.find("anthropic", "unknown") is None
    assert prices.find("bedrock", "claude-haiku-4-5") is None


def test_a_hold_uses_the_dearest_candidate_and_missing_prices_are_listed():
    cheap = accounting.Price(Decimal(1), Decimal(1), Decimal(1), Decimal(1))
    prices = accounting.Prices({("a", "m"): cheap, ("b", "m"): PRICE})
    route = config.Route(
        candidates=[config.Candidate(provider="a", model="m"), config.Candidate(provider="b", model="m")],
        max_tokens_ceiling=100,
    )
    assert prices.most(route, 1000, 100) == PRICE.most(1000, 100)
    other = config.Route(candidates=[config.Candidate(provider="c", model="x")], max_tokens_ceiling=1)
    assert prices.missing({"r": route, "o": other}) == ["o: c/x"]


# --- against the database ---


@pytest.fixture
def conn(database_url):
    with psycopg.connect(database_url, autocommit=True) as connection:
        migrate.apply(connection, migrate.load())
        # The stub priced high enough that each request costs something visible.
        connection.execute("update prices set input = 1000, output = 1000, cache_read = 0, cache_write = 1000 "
                           "where model = 'stub-echo'")  # fmt: skip
        connection.execute("insert into prices values ('anthropic', 'claude-test', 1, 5, 0.1, 1.25, '2026-01-01')")
        yield connection


def team_with_key(conn, name="research", budget=None, rpm=None, tpm=None) -> str:
    admin.add_team(conn, name)
    if budget is not None:
        admin.set_budget(conn, name, Decimal(budget))
    key = admin.issue_key(conn, name)
    if rpm or tpm:
        conn.execute(
            "update api_keys set requests_per_minute = coalesce(%s, requests_per_minute), "
            "tokens_per_minute = coalesce(%s, tokens_per_minute) where prefix = %s",
            (rpm, tpm, key[:10]),
        )
    return key


async def started(database_url):
    pool = db.make_pool(database_url)
    await pool.open(wait=True)
    ledger = accounting.Accounting(pool)
    await ledger.start(routes())
    return pool, ledger


def month_row(conn, team="research"):
    return conn.execute(
        "select spent_usd, reserved_usd from budgets b join teams t on t.id = b.team_id where t.name = %s", (team,)
    ).fetchone()


@pytest.mark.anyio
async def test_a_hold_is_replaced_by_the_actual_cost(conn, database_url):
    key = team_with_key(conn, budget="1.00")
    pool, ledger = await started(database_url)
    async with pool:
        team = await keys.PostgresKeyStore(pool).team_for(key)
        hold = await ledger.admit(team, "stub", routes()["stub"], chat(max_tokens=10), "req-1")
        assert month_row(conn) == (Decimal(0), hold.reserved_usd)
        await ledger.settle(hold, outcome="ok", attempts=1, provider="stub", configured_model="stub-echo",
                            model="stub-echo", usage=Usage(input_tokens=2, output_tokens=5))  # fmt: skip
    assert month_row(conn) == (Decimal("0.007"), Decimal(0))
    row = conn.execute("select outcome, cost_usd, reserved_usd, input_tokens, output_tokens, key_prefix from usage")
    assert row.fetchone() == ("ok", Decimal("0.007"), hold.reserved_usd, 2, 5, key[:10])


@pytest.mark.anyio
async def test_over_budget_is_refused_before_any_call_and_holds_nothing(conn, database_url):
    key = team_with_key(conn, budget="0.05")
    pool, ledger = await started(database_url)
    async with pool:
        team = await keys.PostgresKeyStore(pool).team_for(key)
        with pytest.raises(accounting.Limited) as limited:
            await ledger.admit(team, "stub", routes()["stub"], chat(max_tokens=100), "req-1")  # holds ~$0.14
        assert limited.value.code == "budget_exceeded"
        assert month_row(conn) == (Decimal(0), Decimal(0))
        # A smaller max_tokens holds less, and fits.
        await ledger.admit(team, "stub", routes()["stub"], chat(text="hi", max_tokens=1), "req-2")


@pytest.mark.anyio
async def test_failed_requests_cost_nothing_and_broken_streams_cost_their_hold(conn, database_url):
    key = team_with_key(conn)
    pool, ledger = await started(database_url)
    async with pool:
        team = await keys.PostgresKeyStore(pool).team_for(key)
        failed = await ledger.admit(team, "stub", routes()["stub"], chat(max_tokens=10), "req-1")
        await ledger.settle(failed, outcome="failed", attempts=2)
        broken = await ledger.admit(team, "stub", routes()["stub"], chat(max_tokens=10, stream=True), "req-2")
        await ledger.settle(broken, outcome="stream_error", attempts=1, provider="stub")
        await ledger.settle(broken, outcome="stream_error", attempts=1)  # settling twice changes nothing
    assert month_row(conn) == (broken.reserved_usd, Decimal(0))
    outcomes = conn.execute("select outcome, cost_usd from usage order by id").fetchall()
    assert outcomes == [("failed", Decimal(0)), ("stream_error", broken.reserved_usd)]


@pytest.mark.anyio
async def test_the_request_bucket_refills_over_time(conn, database_url):
    key = team_with_key(conn, rpm=2)
    pool, ledger = await started(database_url)
    async with pool:
        team = await keys.PostgresKeyStore(pool).team_for(key)
        for i in range(2):
            await ledger.admit(team, "stub", routes()["stub"], chat(max_tokens=1), f"req-{i}")
        with pytest.raises(accounting.Limited) as limited:
            await ledger.admit(team, "stub", routes()["stub"], chat(max_tokens=1), "req-3")
        assert limited.value.code == "rate_limited"
        assert 0 < limited.value.retry_after <= 30  # 2 a minute: one more in under 30 s
        conn.execute("update rate_buckets set refilled_at = refilled_at - interval '30 seconds'")
        await ledger.admit(team, "stub", routes()["stub"], chat(max_tokens=1), "req-4")


@pytest.mark.anyio
async def test_the_token_bucket_takes_the_estimate_and_gives_back_what_wasnt_used(conn, database_url):
    key = team_with_key(conn, tpm=1000)
    pool, ledger = await started(database_url)
    async with pool:
        team = await keys.PostgresKeyStore(pool).team_for(key)
        with pytest.raises(accounting.Limited) as too_big:
            await ledger.admit(team, "stub", routes()["stub"], chat(max_tokens=2000), "req-1")
        assert (too_big.value.status, too_big.value.code) == (400, "request_too_large_for_rate_limit")
        hold = await ledger.admit(team, "stub", routes()["stub"], chat(max_tokens=500), "req-2")
        taken = 1000 - conn.execute("select tokens from rate_buckets").fetchone()[0]
        assert taken == pytest.approx(hold.estimated_tokens, abs=1)
        await ledger.settle(hold, outcome="ok", attempts=1, provider="stub", model="stub-echo",
                            usage=Usage(input_tokens=2, output_tokens=5))  # fmt: skip
        left = conn.execute("select tokens from rate_buckets").fetchone()[0]
        assert left == pytest.approx(1000 - 7, abs=1)


@pytest.mark.anyio
async def test_a_route_model_without_a_price_stops_startup(conn, database_url):
    conn.execute("delete from prices where model = 'claude-test'")
    pool = db.make_pool(database_url)
    await pool.open(wait=True)
    async with pool:
        with pytest.raises(RuntimeError, match="claude: anthropic/claude-test"):
            await accounting.Accounting(pool).start(routes())


def test_budget_changes_are_audited(conn):
    admin.add_team(conn, "research")
    admin.set_budget(conn, "research", Decimal("25"))
    admin.set_budget(conn, "research", None)
    details = [r[0] for r in conn.execute("select detail from audit_log where action = 'budget.changed' order by id")]
    assert details == [{"from": None, "to": "25"}, {"from": "25.00", "to": None}]
    assert admin.spending(conn, "research") == (None, 0, 0)


# --- through the API, under concurrency ---


@pytest.mark.anyio
async def test_fifty_requests_at_once_cannot_overspend_the_budget(conn, database_url, monkeypatch):
    """Each request holds about $0.05 and really costs $0.005. With a $0.50 budget, only about 9 fit at once: the
    rest must be refused, never let through to overspend. Without the hold, all 50 would see room and proceed."""
    key = team_with_key(conn, budget="0.50")
    monkeypatch.setenv("DATABASE_URL", database_url)
    stub = StubProvider(delay=0.05)  # slow enough that requests overlap
    app = gateway.create_app(routes(), {"stub": stub, "anthropic": StubProvider("anthropic")})
    body = {"model": "stub", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]}
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(transport=transport, base_url="http://gw", timeout=30) as client:
            responses = await asyncio.gather(*(
                client.post("/v1/chat/completions", json=body, headers={"authorization": f"Bearer {key}"})
                for _ in range(50)
            ))  # fmt: skip
    codes = [r.status_code for r in responses]
    refused = [r.json()["error"]["code"] for r in responses if r.status_code != 200]
    spent, reserved = month_row(conn)
    ledger_total = conn.execute("select coalesce(sum(cost_usd), 0) from usage").fetchone()[0]
    assert set(refused) <= {"budget_exceeded"}
    assert 0 < codes.count(200) < 50 and refused  # some ran, some were held back
    assert spent <= Decimal("0.50") and reserved == 0
    assert spent == ledger_total == Decimal("0.005") * codes.count(200)
    assert stub.calls == codes.count(200)  # refused requests never reached the provider
