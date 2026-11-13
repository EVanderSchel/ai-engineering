"""Makes the two databases step 10's comparison needs, in the compose PostgreSQL. Run once before the first
`docker compose --profile compare up`, and again any time (it only adds what's missing).

    uv run python src/compare_db.py

- `compare_gateway`: the gateway's own migrations, plus prices for the fake upstream's models ($100 per million tokens
  in and out, the same as compare/litellm.yaml gives LiteLLM), so both gateways' budgets count the same spend. The
  dev database (`gateway`) is left alone.
- `litellm`: empty. LiteLLM creates its own tables when it starts.
"""

import psycopg
from psycopg import conninfo, sql

import migrate

DATABASES = ("compare_gateway", "litellm")
FAKE_MODELS = ("fake-ok", "fake-503", "fake-400", "fake-429", "fake-slow")
PRICE_PER_MILLION = 100  # USD, in and out: high, so a test budget of a few cents (the gateway's budgets are kept
# to the cent) covers only a handful of requests


def url_for(database: str) -> str:
    parts = conninfo.conninfo_to_dict(migrate.database_url())  # the dev database's URL, from .env
    return conninfo.make_conninfo(**(parts | {"dbname": database}))


def main() -> None:
    with psycopg.connect(migrate.database_url(), autocommit=True) as conn:
        existing = {row[0] for row in conn.execute("select datname from pg_database")}
        for name in DATABASES:
            if name not in existing:
                conn.execute(sql.SQL("create database {}").format(sql.Identifier(name)))
                print(f"created database {name}")
    with psycopg.connect(url_for("compare_gateway"), autocommit=True) as conn:  # apply() needs autocommit
        applied = migrate.apply(conn, migrate.load())
        print(f"compare_gateway: {len(applied)} migrations applied")
        with conn.transaction():
            for model in FAKE_MODELS:
                conn.execute(
                    """insert into prices (provider, model, input, output, cache_read, cache_write, effective_from)
                       values ('local', %s, %s, %s, 0, 0, '2026-01-01')
                       on conflict (provider, model, effective_from) do update
                       set input = excluded.input, output = excluded.output""",
                    (model, PRICE_PER_MILLION, PRICE_PER_MILLION),
                )
    print("ready: docker compose --profile compare up -d --build --wait")


if __name__ == "__main__":
    main()
