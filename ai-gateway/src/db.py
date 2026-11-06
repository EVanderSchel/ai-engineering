"""The gateway's connection pool to PostgreSQL.

Plain language: opening a database connection takes a round trip and some work on the server, so the gateway keeps
a few open and lends them to requests, like a library lending books instead of printing one per reader. The pool
opens when the app starts and closes when it stops.
"""

import asyncio
import contextlib
import time

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


class Health:
    """Whether the database answers, checked in the background, for /readyz (step 8 stage 5).

    Plain language: Kubernetes asks every few seconds "can you serve?". Answering by borrowing a connection from the
    request pool meant that a busy pod, whose connections were all lent out to queued requests, said "no" and was
    taken out of service: capacity lost exactly when it was most needed (stage 5's load test). So the question is
    answered from a note instead: a background task checks the database every `interval` seconds on its own single
    connection, and the pod is ready if the last success is at most `stale_after` seconds old.

    A database that really stops answering still turns the pod not ready, a few seconds later than before (up to
    `stale_after` plus the probe's own period and failure count).
    """

    def __init__(self, url: str, interval: float = 2.0, timeout: float = 2.0, stale_after: float = 6.0,
                 clock=time.monotonic):  # fmt: skip
        self.url, self.interval, self.timeout, self.stale_after, self.clock = url, interval, timeout, stale_after, clock
        self.last_ok: float | None = None
        self._conn: psycopg.AsyncConnection | None = None

    def ready(self) -> bool:
        return self.last_ok is not None and self.clock() - self.last_ok <= self.stale_after

    async def check(self) -> bool:
        """One check; records the time if it succeeded. A failed connection is dropped and reopened next time."""
        try:
            async with asyncio.timeout(self.timeout):
                if self._conn is None or self._conn.closed:
                    self._conn = await psycopg.AsyncConnection.connect(
                        self.url, autocommit=True, connect_timeout=max(1, int(self.timeout))
                    )
                await self._conn.execute("select 1")
        except (TimeoutError, *UNAVAILABLE):
            await self.close()
            return False
        self.last_ok = self.clock()
        return True

    async def run(self) -> None:
        """Checks forever, until cancelled (the app's lifespan does that on shutdown)."""
        while True:
            await self.check()
            await asyncio.sleep(self.interval)

    async def close(self) -> None:
        if self._conn is not None:
            with contextlib.suppress(Exception):
                await self._conn.close()
            self._conn = None


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
