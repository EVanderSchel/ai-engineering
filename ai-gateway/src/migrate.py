"""Apply the database migrations in migrations/ that haven't been applied yet, in order.

    uv run python src/migrate.py           # uses DATABASE_URL (from the environment, else from .env)

Plain language: the database's shape (its tables and rules) is changed only by numbered SQL files, each applied
once, in order, and recorded. Every copy of the database, a laptop's, CI's, or the cluster's, ends up identical,
and the history of every change lives in the repo.

How it stays safe:

- each file runs in its own transaction, so a file that fails leaves no half-applied change;
- a PostgreSQL advisory lock means two gateway replicas starting at once can't both run the same migration;
- each applied file's SHA-256 is recorded, and a file edited after it was applied stops the runner: a change to
  the schema is a new file, never an edit to an old one.
"""

import hashlib
import os
import pathlib
import re
import sys
from dataclasses import dataclass

import psycopg

import paths

MIGRATIONS_DIR = paths.ROOT / "migrations"
NAME = re.compile(r"^(\d{3})_([a-z0-9_]+)\.sql$")
LOCK_ID = 7_401_202_601  # any fixed number: the advisory lock's name for "migrating the gateway's schema"


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        # Line endings normalized: Git on Windows checks files out with CRLF, Linux (CI, the cluster) with LF, and
        # the same migration must have the same checksum everywhere.
        return hashlib.sha256(self.sql.replace("\r\n", "\n").encode("utf-8")).hexdigest()


class MigrationError(RuntimeError):
    pass


def load(directory: pathlib.Path = MIGRATIONS_DIR) -> list[Migration]:
    migrations = []
    for path in sorted(directory.glob("*.sql")):
        match = NAME.match(path.name)
        if not match:
            raise MigrationError(f"{path.name}: migration files are named NNN_snake_case.sql")
        migrations.append(Migration(int(match[1]), match[2], path.read_text(encoding="utf-8")))
    versions = [m.version for m in migrations]
    if versions != list(range(1, len(migrations) + 1)):
        raise MigrationError(f"migration numbers must run 001, 002, ... without gaps; found {versions}")
    return migrations


def apply(conn: psycopg.Connection, migrations: list[Migration]) -> list[Migration]:
    """Applies what's missing and returns it. `conn` must be in autocommit mode (each file gets its own transaction)."""
    conn.execute("""
        create table if not exists schema_migrations (
            version     integer primary key,
            name        text not null,
            checksum    char(64) not null,
            applied_at  timestamptz not null default now()
        )
    """)
    conn.execute("select pg_advisory_lock(%s)", (LOCK_ID,))
    try:
        applied = {v: (n, c) for v, n, c in conn.execute("select version, name, checksum from schema_migrations")}
        done = []
        for migration in migrations:
            if migration.version in applied:
                name, checksum = applied[migration.version]
                if checksum != migration.checksum:
                    raise MigrationError(
                        f"{migration.version:03d}_{migration.name}.sql changed after it was applied; "
                        "put schema changes in a new migration instead"
                    )
                continue
            with conn.transaction():
                conn.execute(migration.sql)
                conn.execute(
                    "insert into schema_migrations (version, name, checksum) values (%s, %s, %s)",
                    (migration.version, migration.name, migration.checksum),
                )
            done.append(migration)
        unknown = sorted(set(applied) - {m.version for m in migrations})
        if unknown:
            raise MigrationError(f"the database has migrations this code doesn't know: {unknown} (an older checkout?)")
        return done
    finally:
        conn.execute("select pg_advisory_unlock(%s)", (LOCK_ID,))


def database_url() -> str:
    """DATABASE_URL from the environment, else from the project's .env (local development only)."""
    if url := os.environ.get("DATABASE_URL"):
        return url
    env_file = paths.ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if line.startswith("DATABASE_URL="):
                return line.split("=", 1)[1].strip()
    raise MigrationError("DATABASE_URL isn't set (and there's no .env with it)")


def main() -> None:
    with psycopg.connect(database_url(), autocommit=True, connect_timeout=10) as conn:
        done = apply(conn, load())
    print(f"applied {len(done)} migration(s)" + "".join(f"\n  {m.version:03d}_{m.name}" for m in done))


if __name__ == "__main__":
    try:
        main()
    except MigrationError as error:
        sys.exit(f"migration failed: {error}")
