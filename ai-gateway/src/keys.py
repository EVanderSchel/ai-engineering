"""API keys: a team's key is shown once and only its SHA-256 hash is kept.

Plain language: like a password, the gateway never stores the key itself, only a fingerprint of it. A leaked
configuration file or database dump then gives nobody a working key.

Two places keys can live, behind one interface (a "key store"):

- PostgreSQL (the normal case, step 4): teams and keys are managed with `src/admin.py`; a revoked key stops working
  on the next request, and a team can be limited to certain routes.
- the GATEWAY_KEYS environment variable ("team:hash,team:hash"): for running without a database, as tests and the
  stub-only local setup do. Every route is allowed and nothing can be revoked short of a restart.

    uv run python src/keys.py new <team>    # a key and its GATEWAY_KEYS line (database-free setups only)
"""

import hashlib
import secrets
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

PREFIX = "gw_"
SHOWN_PREFIX = 10  # characters of a key kept in the database and logs to recognise it: "gw_" plus 7


def new_key() -> str:
    return PREFIX + secrets.token_urlsafe(32)


def key_hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def bearer_key(authorization: str | None) -> str | None:
    """The key in an `Authorization: Bearer gw_...` header, or None."""
    if not authorization:
        return None
    scheme, _, key = authorization.partition(" ")
    key = key.strip()
    if scheme.lower() != "bearer" or not key.startswith(PREFIX):
        return None
    return key


@dataclass(frozen=True)
class Team:
    """The team a request's key belongs to, with what that key and team may use. None: no limit (no database)."""

    id: int
    name: str
    allowed_routes: tuple[str, ...] | None = None  # None: every route
    key_id: int | None = None
    key_prefix: str = ""
    requests_per_minute: int | None = None
    tokens_per_minute: int | None = None
    monthly_budget_usd: Decimal | None = None
    guardrails: dict | None = field(default=None, hash=False, compare=False)  # the team's own settings (step 5)

    def may_use(self, route: str) -> bool:
        return self.allowed_routes is None or route in self.allowed_routes


class KeyStore(Protocol):
    async def team_for(self, key: str) -> Team | None:
        """The team a valid, unrevoked key belongs to, or None."""
        ...


def parse_keys(value: str) -> dict[str, str]:
    """GATEWAY_KEYS ("team:hash,team:hash") as {hash: team}."""
    keys = {}
    for entry in filter(None, (part.strip() for part in value.split(","))):
        team, sep, digest = entry.partition(":")
        if not sep or not team or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError(f"GATEWAY_KEYS entry {entry[:20]!r}... is not team:<sha256 hex>")
        keys[digest] = team
    return keys


class MemoryKeyStore:
    """Keys from GATEWAY_KEYS, held in memory. No database, no revocation, every route allowed."""

    def __init__(self, hashes: dict[str, str]):
        self.hashes = hashes

    async def team_for(self, key: str) -> Team | None:
        name = self.hashes.get(key_hash(key))
        return Team(0, name) if name else None


class PostgresKeyStore:
    """Keys in the api_keys table, checked on every request, so a revocation takes effect at once."""

    def __init__(self, pool):
        self.pool = pool  # a psycopg_pool.AsyncConnectionPool

    async def team_for(self, key: str) -> Team | None:
        async with self.pool.connection() as conn:
            row = await (
                await conn.execute(
                    """
                    select t.id, t.name, t.allowed_routes, k.id, k.prefix, k.requests_per_minute,
                           k.tokens_per_minute, t.monthly_budget_usd, t.guardrails
                    from api_keys k join teams t on t.id = k.team_id
                    where k.key_hash = %s and k.revoked_at is null
                    """,
                    (key_hash(key),),
                )
            ).fetchone()
        if row is None:
            return None
        team_id, name, routes, key_id, prefix, rpm, tpm, budget, guardrails = row
        routes = tuple(routes) if routes is not None else None
        return Team(team_id, name, routes, key_id, prefix, rpm, tpm, budget, guardrails or None)


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "new":
        sys.exit("usage: python src/keys.py new <team>")
    key = new_key()
    print(f"Key for {sys.argv[2]} (shown once; give it to the team, don't store it):\n  {key}")
    print(f"Add to GATEWAY_KEYS:\n  {sys.argv[2]}:{key_hash(key)}")
