-- Teams and their API keys (design section 5). Budgets, rate limits, prices and the usage ledger come in 002.
--
-- A key itself is never stored: only its SHA-256 hash (to check it) and a short prefix (to recognise it in logs and
-- in the admin commands without revealing it).

create table teams (
    id              bigint generated always as identity primary key,
    name            text not null unique check (name ~ '^[a-z0-9][a-z0-9-]{0,62}$'),
    allowed_routes  text[],  -- null: every route
    created_at      timestamptz not null default now()
);

create table api_keys (
    id          bigint generated always as identity primary key,
    team_id     bigint not null references teams (id),
    key_hash    char(64) not null unique check (key_hash ~ '^[0-9a-f]{64}$'),
    prefix      text not null,
    created_at  timestamptz not null default now(),
    revoked_at  timestamptz  -- null: the key works
);

create index api_keys_team on api_keys (team_id);
