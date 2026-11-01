"""Admin commands for teams and API keys, run against the gateway's database. Every change is written to the
audit log in the same transaction as the change itself.

    uv run python src/admin.py team add research --routes smart,fast    # --routes omitted: every route
    uv run python src/admin.py team list
    uv run python src/admin.py team budget research 25                  # USD a month; 'none' removes the cap
    uv run python src/admin.py team spend research                      # this month: spent, held, budget
    uv run python src/admin.py team guardrails research injection=block injection_judge=true pii_input=redact
    uv run python src/admin.py team guardrails research --clear         # back to the routes' own settings
    uv run python src/admin.py key issue research                      # prints the key once
    uv run python src/admin.py key list research
    uv run python src/admin.py key revoke gw_AbC1234                    # by the prefix `key list` shows

Plain language: there's no sign-up page; an administrator creates a team and hands its key over, the way a building
manager issues badges. The key is printed once and never stored, only its fingerprint.
"""

import argparse
import getpass
import sys
from decimal import Decimal, InvalidOperation

import psycopg
from psycopg.types.json import Jsonb

import config
import keys
import migrate


class AdminError(RuntimeError):
    pass


def actor() -> str:
    return f"admin:{getpass.getuser()}"


def audit(conn: psycopg.Connection, action: str, team_id: int | None, detail: dict) -> None:
    conn.execute(
        "insert into audit_log (actor, action, team_id, detail) values (%s, %s, %s, %s)",
        (actor(), action, team_id, Jsonb(detail)),
    )


def team_id(conn: psycopg.Connection, name: str) -> int:
    row = conn.execute("select id from teams where name = %s", (name,)).fetchone()
    if row is None:
        raise AdminError(f"no team named {name!r}")
    return row[0]


def add_team(conn: psycopg.Connection, name: str, routes: list[str] | None = None) -> int:
    with conn.transaction():
        try:
            (new_id,) = conn.execute(
                "insert into teams (name, allowed_routes) values (%s, %s) returning id", (name, routes)
            ).fetchone()
        except psycopg.errors.UniqueViolation:
            raise AdminError(f"a team named {name!r} already exists") from None
        except psycopg.errors.CheckViolation:
            raise AdminError(f"{name!r}: team names are lowercase letters, digits and dashes") from None
        audit(conn, "team.created", new_id, {"name": name, "allowed_routes": routes})
    return new_id


def parse_guardrails(pairs: list[str]) -> dict:
    """`key=value` pairs into a team's guardrail settings, checked against config.Guardrails. Only the settings
    given are stored, so the routes' values apply to the rest (each setting ends at the stricter of the two)."""
    settings: dict = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep:
            raise AdminError(f"{pair!r}: settings are key=value, e.g. injection=block")
        if key == "pii_kinds":
            settings[key] = [k.strip() for k in value.split(",") if k.strip()]
        elif key == "injection_judge":
            if value.lower() not in ("true", "false"):
                raise AdminError("injection_judge is true or false")
            settings[key] = value.lower() == "true"
        else:
            settings[key] = value
    try:
        config.Guardrails.model_validate(settings)
    except ValueError as error:
        raise AdminError(f"invalid guardrail settings: {error}") from None
    return settings


def set_guardrails(conn: psycopg.Connection, team: str, settings: dict) -> None:
    with conn.transaction():
        tid = team_id(conn, team)
        (old,) = conn.execute("select guardrails from teams where id = %s", (tid,)).fetchone()
        conn.execute("update teams set guardrails = %s where id = %s", (Jsonb(settings), tid))
        audit(conn, "guardrails.changed", tid, {"from": old, "to": settings})


def set_budget(conn: psycopg.Connection, team: str, usd: Decimal | None) -> None:
    """The team's monthly budget in USD (None: no cap). Applies to the current month at once."""
    if usd is not None and usd < 0:
        raise AdminError("a budget can't be negative")
    with conn.transaction():
        tid = team_id(conn, team)
        (old,) = conn.execute("select monthly_budget_usd from teams where id = %s", (tid,)).fetchone()
        conn.execute("update teams set monthly_budget_usd = %s where id = %s", (usd, tid))
        audit(
            conn,
            "budget.changed",
            tid,
            {"from": str(old) if old is not None else None, "to": str(usd) if usd is not None else None},
        )


def spending(conn: psycopg.Connection, team: str) -> tuple:
    """This month's (budget, spent, held for running requests) for the team."""
    return conn.execute(
        """
        select t.monthly_budget_usd, coalesce(b.spent_usd, 0), coalesce(b.reserved_usd, 0)
        from teams t left join budgets b on b.team_id = t.id and b.month = date_trunc('month', now() at time zone 'utc')
        where t.name = %s
        """,
        (team,),
    ).fetchone()


def list_teams(conn: psycopg.Connection) -> list[tuple]:
    return conn.execute(
        """
        select t.name, coalesce(array_to_string(t.allowed_routes, ','), 'all'),
               count(k.id) filter (where k.revoked_at is null), t.created_at
        from teams t left join api_keys k on k.team_id = t.id
        group by t.id order by t.name
        """
    ).fetchall()


def issue_key(conn: psycopg.Connection, team: str) -> str:
    """A new key for the team. The return value is the only time the key exists outside the caller's hands."""
    key = keys.new_key()
    prefix = key[: keys.SHOWN_PREFIX]
    with conn.transaction():
        tid = team_id(conn, team)
        conn.execute(
            "insert into api_keys (team_id, key_hash, prefix) values (%s, %s, %s)", (tid, keys.key_hash(key), prefix)
        )
        audit(conn, "key.issued", tid, {"prefix": prefix})
    return key


def list_keys(conn: psycopg.Connection, team: str) -> list[tuple]:
    return conn.execute(
        "select prefix, created_at, revoked_at from api_keys where team_id = %s order by created_at",
        (team_id(conn, team),),
    ).fetchall()


def revoke_key(conn: psycopg.Connection, prefix: str) -> None:
    with conn.transaction():
        rows = conn.execute(
            "select id, team_id from api_keys where prefix = %s and revoked_at is null", (prefix,)
        ).fetchall()
        if not rows:
            raise AdminError(f"no active key with prefix {prefix!r}")
        if len(rows) > 1:  # 7 random characters: a clash is very unlikely, but never revoke the wrong key
            raise AdminError(f"{len(rows)} active keys share prefix {prefix!r}; revoke by id in psql instead")
        key_id, tid = rows[0]
        conn.execute("update api_keys set revoked_at = now() where id = %s", (key_id,))
        audit(conn, "key.revoked", tid, {"prefix": prefix})


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="admin.py", description="Manage the gateway's teams and API keys.")
    groups = parser.add_subparsers(dest="group", required=True)
    team = groups.add_parser("team").add_subparsers(dest="command", required=True)
    add = team.add_parser("add")
    add.add_argument("name")
    add.add_argument("--routes", help="comma-separated route names the team may use (default: every route)")
    team.add_parser("list")
    budget = team.add_parser("budget", help="set a team's monthly budget in USD ('none': no cap)")
    budget.add_argument("name")
    budget.add_argument("usd")
    team.add_parser("spend").add_argument("name")
    guard = team.add_parser("guardrails", help="tighten a team's guardrails: key=value ... (or --clear)")
    guard.add_argument("name")
    guard.add_argument("settings", nargs="*")
    guard.add_argument("--clear", action="store_true")
    key = groups.add_parser("key").add_subparsers(dest="command", required=True)
    key.add_parser("issue").add_argument("team")
    key.add_parser("list").add_argument("team")
    key.add_parser("revoke").add_argument("prefix")
    args = parser.parse_args(argv)

    with psycopg.connect(migrate.database_url(), autocommit=True, connect_timeout=10) as conn:
        match (args.group, args.command):
            case ("team", "add"):
                routes = [r.strip() for r in args.routes.split(",") if r.strip()] if args.routes else None
                add_team(conn, args.name, routes)
                print(f"team {args.name} created (routes: {', '.join(routes) if routes else 'all'})")
            case ("team", "list"):
                for name, routes, active, created in list_teams(conn):
                    print(f"{name:20} routes={routes:20} active keys={active}  created {created:%Y-%m-%d}")
            case ("team", "budget"):
                try:
                    usd = None if args.usd.lower() == "none" else Decimal(args.usd)
                except InvalidOperation:
                    raise AdminError(f"{args.usd!r} isn't an amount (or 'none')") from None
                set_budget(conn, args.name, usd)
                print(f"team {args.name} budget: {'no cap' if usd is None else f'${usd} a month'}")
            case ("team", "guardrails"):
                if args.clear == bool(args.settings):
                    raise AdminError("give key=value settings, or --clear (not both)")
                settings = {} if args.clear else parse_guardrails(args.settings)
                set_guardrails(conn, args.name, settings)
                print(f"team {args.name} guardrails: {settings or "the routes' own settings"}")
            case ("team", "spend"):
                if (row := spending(conn, args.name)) is None:
                    raise AdminError(f"no team named {args.name!r}")
                cap, spent, held = row
                print(f"{args.name} this month: spent ${spent:.4f}, held ${held:.4f}, budget "
                      f"{'no cap' if cap is None else f'${cap}'}")  # fmt: skip
            case ("key", "issue"):
                new = issue_key(conn, args.team)
                print(f"key for {args.team} (shown once: hand it over, don't store it):\n  {new}")
            case ("key", "list"):
                for prefix, created, revoked in list_keys(conn, args.team):
                    state = f"revoked {revoked:%Y-%m-%d %H:%M}" if revoked else "active"
                    print(f"{prefix}...  created {created:%Y-%m-%d %H:%M}  {state}")
            case ("key", "revoke"):
                revoke_key(conn, args.prefix)
                print(f"key {args.prefix}... revoked")


if __name__ == "__main__":
    try:
        main()
    except (AdminError, migrate.MigrationError) as error:
        sys.exit(f"error: {error}")
    except psycopg.errors.UndefinedTable:
        sys.exit("error: the database has no gateway tables yet; run src/migrate.py first")
