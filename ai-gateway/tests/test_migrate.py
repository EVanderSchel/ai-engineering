"""The migrations runner and the schema it builds. The database tests need PostgreSQL (see conftest.database_url);
the file checks don't."""

import psycopg
import pytest

import migrate


def connect(url):
    return psycopg.connect(url, autocommit=True)


# --- the migration files themselves (no database) ---


def test_the_committed_migrations_are_numbered_without_gaps():
    migrations = migrate.load()
    assert [m.version for m in migrations] == list(range(1, len(migrations) + 1))
    assert migrations[0].name == "teams_and_keys"


def test_badly_named_or_gapped_files_are_refused(tmp_path):
    (tmp_path / "001_first.sql").write_text("select 1;")
    (tmp_path / "003_third.sql").write_text("select 1;")
    with pytest.raises(migrate.MigrationError, match="without gaps"):
        migrate.load(tmp_path)
    (tmp_path / "003_third.sql").unlink()
    (tmp_path / "2-second.sql").write_text("select 1;")
    with pytest.raises(migrate.MigrationError, match="NNN_snake_case"):
        migrate.load(tmp_path)


def test_line_endings_dont_change_a_checksum():
    lf = migrate.Migration(1, "x", "create table t (id int);\nselect 1;\n")
    crlf = migrate.Migration(1, "x", "create table t (id int);\r\nselect 1;\r\n")
    assert lf.checksum == crlf.checksum


# --- against a real database ---


def test_migrations_apply_once_and_are_recorded(database_url):
    with connect(database_url) as conn:
        migrations = migrate.load()
        first = migrate.apply(conn, migrations)
        again = migrate.apply(conn, migrations)
        recorded = conn.execute("select version, name from schema_migrations order by version").fetchall()
    assert first == migrations
    assert again == []
    assert recorded == [(m.version, m.name) for m in migrations]


def test_a_failing_migration_leaves_nothing_half_applied(database_url):
    broken = [migrate.Migration(1, "broken", "create table half (id int); select * from no_such_table;")]
    with connect(database_url) as conn:
        with pytest.raises(psycopg.errors.UndefinedTable):
            migrate.apply(conn, broken)
        assert conn.execute("select to_regclass('half')").fetchone() == (None,)
        assert conn.execute("select count(*) from schema_migrations").fetchone() == (0,)


def test_editing_an_applied_migration_is_refused(database_url):
    with connect(database_url) as conn:
        migrate.apply(conn, [migrate.Migration(1, "t", "create table t (id int);")])
        with pytest.raises(migrate.MigrationError, match="changed after it was applied"):
            migrate.apply(conn, [migrate.Migration(1, "t", "create table t (id bigint);")])


def test_a_database_ahead_of_the_code_is_refused(database_url):
    with connect(database_url) as conn:
        migrate.apply(conn, [migrate.Migration(1, "a", "select 1;"), migrate.Migration(2, "b", "select 1;")])
        with pytest.raises(migrate.MigrationError, match="doesn't know"):
            migrate.apply(conn, [migrate.Migration(1, "a", "select 1;")])


def test_the_schema_enforces_its_rules(database_url):
    with connect(database_url) as conn:
        migrate.apply(conn, migrate.load())
        team_id = conn.execute("insert into teams (name) values ('research') returning id").fetchone()[0]
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("insert into teams (name) values ('Bad Name!')")
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute("insert into teams (name) values ('research')")
        good_hash = "a" * 64
        conn.execute("insert into api_keys (team_id, key_hash, prefix) values (%s, %s, 'gw_abc')", (team_id, good_hash))
        with pytest.raises(psycopg.errors.UniqueViolation):
            conn.execute(
                "insert into api_keys (team_id, key_hash, prefix) values (%s, %s, 'gw_abc')", (team_id, good_hash)
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("insert into api_keys (team_id, key_hash, prefix) values (%s, 'not-a-hash', 'x')", (team_id,))
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            conn.execute("insert into api_keys (team_id, key_hash, prefix) values (999, %s, 'x')", ("b" * 64,))
