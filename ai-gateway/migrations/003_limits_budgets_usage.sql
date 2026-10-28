-- Rate limits per key, monthly budgets per team, the price list and the usage ledger (design section 5).

-- Limits: per key, refilled continuously (a token bucket). Null budget: no monthly cap.
alter table api_keys
    add column requests_per_minute integer not null default 60 check (requests_per_minute > 0),
    add column tokens_per_minute integer not null default 200000 check (tokens_per_minute > 0);

alter table teams
    add column monthly_budget_usd numeric(12, 2) check (monthly_budget_usd >= 0);

-- A key's two buckets: requests and tokens left, as of `refilled_at`. Updated in one statement per request, so
-- gateway replicas share them (decision 2).
create table rate_buckets (
    key_id       bigint primary key references api_keys (id),
    requests     double precision not null,
    tokens       double precision not null,
    refilled_at  timestamptz not null
);

-- A team's month: what's been spent, and what's held for requests still running (decision 12).
create table budgets (
    team_id       bigint not null references teams (id),
    month         date not null,
    spent_usd     numeric(14, 6) not null default 0 check (spent_usd >= 0),
    reserved_usd  numeric(14, 6) not null default 0 check (reserved_usd >= 0),
    primary key (team_id, month)
);

-- USD per million tokens. A price change is a new row from a date: old usage keeps the price it was charged.
-- `model` is the id the provider reports, so dated snapshot ids get their own rows.
create table prices (
    provider        text not null,
    model           text not null,
    input           numeric(10, 4) not null check (input >= 0),
    output          numeric(10, 4) not null check (output >= 0),
    cache_read      numeric(10, 4) not null check (cache_read >= 0),
    cache_write     numeric(10, 4) not null check (cache_write >= 0),
    effective_from  date not null,
    primary key (provider, model, effective_from)
);

-- Prices as of 2026-10-28. Claude API: Anthropic's list prices (5-minute cache write = 1.25 x input). Bedrock: the
-- us-east-2 on-demand rates for the `us.` inference profiles (AWS's public price list, read 2026-10-20 for
-- grant-review-workflow). Claude Sonnet 5 is where the Claude API's server-side refusal fallback sends Sonnet 5.5.
insert into prices (provider, model, input, output, cache_read, cache_write, effective_from) values
    ('anthropic', 'claude-sonnet-5-5',                             2.00, 10.00, 0.20, 2.50,   '2026-01-01'),
    ('anthropic', 'claude-sonnet-5',                               2.00, 10.00, 0.20, 2.50,   '2026-01-01'),
    ('anthropic', 'claude-haiku-4-5',                              1.00,  5.00, 0.10, 1.25,   '2026-01-01'),
    ('anthropic', 'claude-haiku-4-5-20251001',                     1.00,  5.00, 0.10, 1.25,   '2026-01-01'),
    ('bedrock',   'us.anthropic.claude-sonnet-4-6',                3.30, 16.50, 0.33, 4.125,  '2026-01-01'),
    ('bedrock',   'us.anthropic.claude-haiku-4-5-20251001-v1:0',   1.10,  5.50, 0.11, 1.375,  '2026-01-01'),
    ('stub',      'stub-echo',                                     0,     0,    0,    0,      '2026-01-01');

-- One row per admitted request: who, which route, which model answered, tokens, cost, and how it ended. Requests
-- refused by a rate limit or the budget never reach a provider and get no row (step 7 counts them as metrics).
create table usage (
    id              bigint generated always as identity primary key,
    at              timestamptz not null default now(),
    request_id      text not null,
    team_id         bigint not null references teams (id),
    key_prefix      text not null,
    route           text not null,
    provider        text,            -- null: no candidate answered
    model           text,
    attempts        integer not null,
    stream          boolean not null,
    input_tokens    integer not null default 0,
    output_tokens   integer not null default 0,
    cache_read_tokens   integer not null default 0,
    cache_write_tokens  integer not null default 0,
    reserved_usd    numeric(14, 6) not null,
    cost_usd        numeric(14, 6) not null,
    latency_ms      integer not null,
    outcome         text not null     -- ok, failed (no candidate answered), stream_error (broke partway)
);

create index usage_team_at on usage (team_id, at);
