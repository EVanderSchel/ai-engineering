-- Who changed what (design section 5): team and key changes from the admin commands now, budget changes and
-- guardrail blocks in later steps. Rows are only ever added.

create table audit_log (
    id       bigint generated always as identity primary key,
    at       timestamptz not null default now(),
    actor    text not null,   -- who made the change: the admin command's user, or "gateway" for automatic events
    action   text not null,   -- e.g. team.created, key.issued, key.revoked
    team_id  bigint references teams (id),
    detail   jsonb not null default '{}'
);

create index audit_log_team_at on audit_log (team_id, at);
