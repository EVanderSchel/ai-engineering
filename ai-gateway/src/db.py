"""The gateway's connection pool to PostgreSQL.

Plain language: opening a database connection takes a round trip and some work on the server, so the gateway keeps
a few open and lends them to requests, like a library lending books instead of printing one per reader. The pool
opens when the app starts and closes when it stops.
"""

import asyncio

import psycopg
import psycopg_pool

import migrate

# The errors that mean "the database can't be reached right now", as opposed to a bug in a query.
UNAVAILABLE = (psycopg_pool.PoolTimeout, psycopg.OperationalError)


def make_pool(url: str, max_size: int = 10) -> psycopg_pool.AsyncConnectionPool:
    """Not opened yet: the app opens it on startup. Autocommit: each statement is its own transaction unless code
    asks for a longer one with `async with conn.transaction()`."""
    return psycopg_pool.AsyncConnectionPool(
        url,
        min_size=1,
        max_size=max_size,
        open=False,
        timeout=5,  # seconds a request waits for a free connection before giving up
        kwargs={"autocommit": True, "connect_timeout": 5},
    )


async def ping(pool: psycopg_pool.AsyncConnectionPool, timeout: float = 2.0) -> None:
    """Raises one of UNAVAILABLE (or TimeoutError, mapped to it) if the database doesn't answer within `timeout`."""
    try:
        async with asyncio.timeout(timeout), pool.connection(timeout=timeout) as conn:
            await conn.execute("select 1")
    except TimeoutError:
        raise psycopg_pool.PoolTimeout(f"no answer from the database in {timeout} s") from None


async def check_schema(pool: psycopg_pool.AsyncConnectionPool) -> None:
    """The database must have exactly this code's migrations. Behind: run src/migrate.py first. Ahead: this is older
    code than the database expects. Either way, starting would fail later in confusing ways, so it fails now."""
    expected = [(m.version, m.name) for m in migrate.load()]
    async with pool.connection() as conn:
        try:
            rows = await (await conn.execute("select version, name from schema_migrations order by version")).fetchall()
        except psycopg.errors.UndefinedTable:
            rows = []
    if rows != expected:
        missing = [f"{v:03d}_{n}" for v, n in expected if (v, n) not in rows]
        unknown = [f"{v:03d}_{n}" for v, n in rows if (v, n) not in expected]
        raise RuntimeError(
            "the database schema doesn't match this code"
            + (f"; not applied yet (run src/migrate.py): {missing}" if missing else "")
            + (f"; applied but unknown to this code: {unknown}" if unknown else "")
        )
