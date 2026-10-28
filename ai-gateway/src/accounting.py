"""What a request may use and what it cost: rate limits per key, the monthly budget hold per team, the price list,
and the usage ledger (design section 5, decisions 2 and 12).

The order for every request:

1. **Admit**: take one request and the request's estimated tokens from the key's two token buckets (requests per
   minute, tokens per minute). Empty bucket: 429 `rate_limited`, with how long until it refills.
2. **Reserve**: hold the most the request could cost (estimated input plus all of max_tokens, at the dearest
   candidate's price) against the team's month. Not enough left: 429 `budget_exceeded`.
3. The router calls a provider.
4. **Settle**: replace the hold with the actual cost, give back unused tokens to the bucket, and write one usage
   row. A stream that breaks partway has no usage report, so it's charged its whole hold: never under, sometimes
   over.

Plain language for the budget hold: a hotel holds an amount on your card at check-in and charges the real bill at
check-out. Without the hold, fifty requests arriving at once could each see room in the budget and together
overspend it. Both the buckets and the hold are single SQL statements that check and change a row at once, so they
hold across every gateway replica (no counter lives in one replica's memory).
"""

import asyncio
import datetime as dt
import logging
import math
from dataclasses import dataclass, field
from decimal import Decimal

import config
import keys
from providers import Usage
from schemas import ChatRequest

log = logging.getLogger("gateway.accounting")
MILLION = Decimal(1_000_000)


@dataclass(frozen=True)
class Price:
    """USD per million tokens."""

    input: Decimal
    output: Decimal
    cache_read: Decimal
    cache_write: Decimal

    def cost(self, usage: Usage) -> Decimal:
        return (
            usage.input_tokens * self.input
            + usage.output_tokens * self.output
            + usage.cache_read_tokens * self.cache_read
            + usage.cache_write_tokens * self.cache_write
        ) / MILLION

    def most(self, input_tokens: int, output_tokens: int) -> Decimal:
        """The dearest these tokens could be: every input token at the higher of the plain and cache-write rates."""
        return (input_tokens * max(self.input, self.cache_write) + output_tokens * self.output) / MILLION


def estimate_input_tokens(request: ChatRequest) -> int:
    """A deliberately generous estimate (about 3 characters per token, plus per-message overhead): it only sizes the
    hold and the token bucket, and the real count replaces it when the request settles."""
    chars = sum(len(m.text()) for m in request.messages)
    return math.ceil(chars / 3) + 8 * len(request.messages) + 32


class Limited(Exception):
    """The request may not run: rate limited or over budget. Becomes a 429 (or a 400 if it can never fit)."""

    def __init__(self, code: str, message: str, retry_after: float | None = None, status: int = 429):
        super().__init__(message)
        self.code, self.message, self.retry_after, self.status = code, message, retry_after, status


@dataclass
class Hold:
    """One request's claim, from admission to settlement."""

    team: keys.Team
    route: str
    request_id: str
    stream: bool
    estimated_tokens: int  # taken from the token bucket
    reserved_usd: Decimal
    month: dt.date | None
    started: float = field(default_factory=lambda: asyncio.get_running_loop().time())
    settled: bool = False


class Prices:
    def __init__(self, rows: dict[tuple[str, str], Price]):
        self.rows = rows

    def find(self, provider: str, *models: str) -> Price | None:
        """The first of `models` with a price: the model the provider reported, then the one the route named."""
        for model in models:
            if model and (provider, model) in self.rows:
                return self.rows[(provider, model)]
        return None

    def missing(self, routes: dict[str, config.Route]) -> list[str]:
        return [
            f"{name}: {c.provider}/{c.model}"
            for name, route in routes.items()
            for c in route.candidates
            if self.find(c.provider, c.model) is None
        ]

    def most(self, route: config.Route, input_tokens: int, output_tokens: int) -> Decimal:
        return max(self.find(c.provider, c.model).most(input_tokens, output_tokens) for c in route.candidates)


class NoAccounting:
    """For running without a database (tests, stub-only setups): no limits, no budgets, no ledger."""

    async def start(self, routes: dict[str, config.Route]) -> None:
        pass

    async def admit(self, team, route_name, route, request, request_id) -> Hold:
        return Hold(team, route_name, request_id, request.stream, 0, Decimal(0), None)

    async def settle(self, hold: Hold, **_outcome) -> None:
        hold.settled = True


class Accounting:
    def __init__(self, pool):
        self.pool = pool
        self.prices = Prices({})

    async def start(self, routes: dict[str, config.Route]) -> None:
        """Load the price list and refuse to start if any route's model has no price: it couldn't be budgeted."""
        today = dt.datetime.now(dt.UTC).date()
        async with self.pool.connection() as conn:
            rows = await (
                await conn.execute(
                    """
                    select distinct on (provider, model) provider, model, input, output, cache_read, cache_write
                    from prices where effective_from <= %s
                    order by provider, model, effective_from desc
                    """,
                    (today,),
                )
            ).fetchall()
        self.prices = Prices({(p, m): Price(*amounts) for p, m, *amounts in rows})
        if missing := self.prices.missing(routes):
            raise RuntimeError(f"no price for these route candidates (add rows to the prices table): {missing}")

    async def admit(self, team: keys.Team, route_name: str, route: config.Route, request: ChatRequest, request_id: str):
        """Rate limits, then the budget hold. Raises Limited; returns the Hold to settle later."""
        tokens = estimate_input_tokens(request) + request.output_limit()
        if team.tokens_per_minute is not None and tokens > team.tokens_per_minute:
            raise Limited(
                "request_too_large_for_rate_limit",
                f"this request may use up to {tokens} tokens, more than the key's {team.tokens_per_minute} per minute",
                status=400,
            )
        async with self.pool.connection() as conn:
            if team.requests_per_minute is not None:
                await self._take_from_buckets(conn, team, tokens)
            reserved = self.prices.most(route, estimate_input_tokens(request), request.output_limit())
            month = dt.datetime.now(dt.UTC).date().replace(day=1)
            try:
                await self._reserve(conn, team, month, reserved)
            except Limited:
                await self._refund_tokens(conn, team, tokens, requests=1)
                raise
        return Hold(team, route_name, request_id, request.stream, tokens, reserved, month)

    async def _take_from_buckets(self, conn, team: keys.Team, tokens: int) -> None:
        refill = """
            least(%(rpm)s, b.requests + extract(epoch from now() - b.refilled_at) * %(rpm)s / 60.0)
        """
        refill_tokens = """
            least(%(tpm)s, b.tokens + extract(epoch from now() - b.refilled_at) * %(tpm)s / 60.0)
        """
        params = {"key": team.key_id, "rpm": team.requests_per_minute, "tpm": team.tokens_per_minute, "cost": tokens}
        taken = await (
            await conn.execute(
                f"""
                insert into rate_buckets as b (key_id, requests, tokens, refilled_at)
                values (%(key)s, %(rpm)s - 1, %(tpm)s - %(cost)s, now())
                on conflict (key_id) do update set
                    requests = {refill} - 1, tokens = {refill_tokens} - %(cost)s, refilled_at = now()
                where {refill} >= 1 and {refill_tokens} >= %(cost)s
                returning 1
                """,
                params,
            )
        ).fetchone()
        if taken is None:
            requests, tokens_left = await (
                await conn.execute(
                    f"select {refill}, {refill_tokens} from rate_buckets b where key_id = %(key)s", params
                )
            ).fetchone()
            wait = max(
                (1 - requests) / (team.requests_per_minute / 60),
                (tokens - tokens_left) / (team.tokens_per_minute / 60),
                0.0,
            )
            raise Limited(
                "rate_limited",
                f"rate limit reached ({team.requests_per_minute} requests and {team.tokens_per_minute} tokens per "
                "minute for this key)",
                retry_after=wait,
            )

    async def _refund_tokens(self, conn, team: keys.Team, tokens: int, requests: int = 0) -> None:
        if team.key_id is None or team.requests_per_minute is None or (tokens <= 0 and requests <= 0):
            return
        await conn.execute(
            """
            update rate_buckets
            set tokens = least(%s, tokens + %s), requests = least(%s, requests + %s)
            where key_id = %s
            """,
            (team.tokens_per_minute, tokens, team.requests_per_minute, requests, team.key_id),
        )

    async def _reserve(self, conn, team: keys.Team, month: dt.date, amount: Decimal) -> None:
        await conn.execute(
            "insert into budgets (team_id, month) values (%s, %s) on conflict do nothing", (team.id, month)
        )
        if team.monthly_budget_usd is None:
            await conn.execute(
                "update budgets set reserved_usd = reserved_usd + %s where team_id = %s and month = %s",
                (amount, team.id, month),
            )
            return
        held = await (
            await conn.execute(
                """
                update budgets set reserved_usd = reserved_usd + %(amount)s
                where team_id = %(team)s and month = %(month)s
                  and spent_usd + reserved_usd + %(amount)s <= %(limit)s
                returning 1
                """,
                {"amount": amount, "team": team.id, "month": month, "limit": team.monthly_budget_usd},
            )
        ).fetchone()
        if held is None:
            raise Limited(
                "budget_exceeded",
                f"team {team.name!r} has too little of its ${team.monthly_budget_usd} monthly budget left for this "
                f"request (it could cost up to ${amount:.4f}; a smaller max_tokens holds less)",
            )

    async def settle(
        self,
        hold: Hold,
        *,
        outcome: str,
        attempts: int,
        provider: str | None = None,
        configured_model: str | None = None,
        model: str | None = None,
        usage: Usage | None = None,
    ) -> None:
        """Charge the actual cost (or the whole hold if it can't be known), give back unused tokens, write the
        usage row. Never raises: a failure here is logged, not shown to a caller whose answer already went out."""
        if hold.settled:
            return
        hold.settled = True
        if usage is not None:
            price = self.prices.find(provider, model, configured_model) if provider else None
            if price is None:
                log.warning("no price for %s/%s: charging the hold", provider, model)
                cost = hold.reserved_usd
            else:
                cost = price.cost(usage)
        elif outcome == "failed":
            cost = Decimal(0)  # nothing answered: errors before any output aren't billed
        else:
            cost = hold.reserved_usd  # a stream that broke partway: its usage is unknown
        used_tokens = (
            usage.input_tokens + usage.output_tokens + usage.cache_read_tokens + usage.cache_write_tokens
            if usage
            else hold.estimated_tokens
        )
        latency_ms = round((asyncio.get_running_loop().time() - hold.started) * 1000)
        try:
            async with self.pool.connection() as conn, conn.transaction():
                await conn.execute(
                    """
                    update budgets set reserved_usd = greatest(reserved_usd - %s, 0), spent_usd = spent_usd + %s
                    where team_id = %s and month = %s
                    """,
                    (hold.reserved_usd, cost, hold.team.id, hold.month),
                )
                await self._refund_tokens(conn, hold.team, max(hold.estimated_tokens - used_tokens, 0))
                u = usage or Usage()
                await conn.execute(
                    """
                    insert into usage (request_id, team_id, key_prefix, route, provider, model, attempts, stream,
                        input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, reserved_usd, cost_usd,
                        latency_ms, outcome)
                    values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        hold.request_id, hold.team.id, hold.team.key_prefix, hold.route, provider, model, attempts,
                        hold.stream, u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_tokens,
                        hold.reserved_usd, cost, latency_ms, outcome,
                    ),
                )  # fmt: skip
        except Exception:
            log.exception("settling request %s failed", hold.request_id)
